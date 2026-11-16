"""Fixture tests for tests/validation/exit_tables.py.

Each check fails on a violating fixture tree and passes on a conforming one. The fixtures are text in string literals.
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import exit_tables
from validation_support import import_aliases, write_fixture_tree

HEADER = "from console import run_main\nimport argparse\n"
MAIN = (
    "def main() -> int:\n"
    "    argparse.ArgumentParser().parse_args()\n"
    "    return 0\n"
    'if __name__ == "__main__":\n'
    "    run_main(main)\n"
)
TABLE = "Exit status:\n  0  done\n  1  a last line FAILED <reason>\n  2  a usage error\n"


def problems(files: dict[str, str]) -> list[str]:
    """The policy's problems for a fixture tree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, files)
        return exit_tables.table_problems(root, [root / name for name in files])


def script(docstring: str, body: str = MAIN, header: str = HEADER) -> str:
    return f'"""{docstring}"""\n{header}{body}'


def test_an_entry_point_whose_table_names_exactly_its_codes_passes() -> None:
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}")}) == []


def test_a_local_that_rebinds_itself_from_itself_is_read_once() -> None:
    # The value reads its own name, so it leads back to itself: a reader with no set of the values it had read
    # followed it until Python's recursion limit. Read once, it names 0 and 3.
    body = (
        "def main(flag) -> int:\n    argparse.ArgumentParser().parse_args()\n    code = 0\n"
        "    code = code if flag else 3\n    return code\n"
        'if __name__ == "__main__":\n    run_main(main)\n'
    )
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}  3  a third\n", body)}) == []
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)}) == [
        'tools/tool.py can exit 3, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
    ]


@pytest.mark.parametrize(
    "rebinding",
    [
        pytest.param("handler = {'a': one}\n    handler = handler['a']", id="a subscript of itself"),
        pytest.param("handler = {'a': one}\n    handler = handler.get('a')", id="a get on itself"),
        pytest.param("handler = one\n    handler = handler", id="itself"),
    ],
)
def test_a_called_local_that_rebinds_itself_from_itself_is_read_once(rebinding: str) -> None:
    # The called name reads its own value back, so a reader with no set of the values it had read followed it until
    # Python's recursion limit. Read once, it names one(), which exits 3.
    body = (
        f"def one() -> int:\n    return 3\ndef main() -> int:\n    argparse.ArgumentParser().parse_args()\n"
        f"    {rebinding}\n    return handler()\n"
        'if __name__ == "__main__":\n    run_main(main)\n'
    )
    table = "Exit status:\n  1  a last line FAILED <reason>\n  2  a usage error\n  3  a third\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body)}) == []


def test_a_called_parameter_given_the_same_argument_from_another_scope_is_read_again() -> None:
    # k reaches itself through f and g, each passing its own h on: g's call k(one) gives k a scope in which h is one,
    # while main's k(two) gives one in which h is two. A reader that knew a scope only by its arguments' expressions
    # read h once and lost one(), and with it exit 3.
    body = (
        "def c():\n    return bool(argparse)\ndef one():\n    return 3\ndef two():\n    return 4\n"
        "def f(h):\n    if c(): return k(h), 1\n    if c(): return h()\n"
        "def g(h):\n    pair = k(one)\n    code, _ = pair\n    if c(): return code\n"
        "def k(h):\n    pair = f(h)\n    pair = k(h)\n    if c(): return pair\n    return g(h)\n"
        "def main():\n    argparse.ArgumentParser().parse_args()\n    return k(two)\n"
        'if __name__ == "__main__":\n    run_main(main)\n'
    )
    found = problems({"tools/tool.py": script("A tool.\n\nExit status:\n  1  a\n", body)})
    assert (
        'tools/tool.py can exit 3, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
        in found
    )


WALK = (
    "from pathlib import Path\n"
    "def check(path: Path, depth: int) -> int:\n    if depth > 4:\n        return 3\n"
    "    if path.is_symlink():\n        return check(path.resolve(), depth + 1)\n"
    "    if path.is_dir():\n        return check(path / '__main__.py', depth + 1)\n"
    "    if not path.exists():\n        return check(path.parent, depth + 1)\n"
    "    if path.suffix != '.py':\n        return check(path.with_suffix('.py'), depth + 1)\n    return 0\n"
    "def main() -> int:\n    arguments = argparse.ArgumentParser()\n    arguments.add_argument('path')\n"
    "    return check(Path(arguments.parse_args().path), 0)\n"
)
CALLED = "def c():\n    return bool(argparse)\ndef one():\n    return 3\ndef two():\n    return 4\n"
SITES = "".join("    if c(): return k(h)\n" for _ in range(6))
CHAIN = "".join(
    f"def f{index}(a0, a1):\n    if c(): return a0()\n    return f{index + 1}(a0, a1)\n" for index in range(20)
)


@pytest.mark.parametrize(
    ("body", "table"),
    [
        pytest.param(WALK, "  0  done\n  1  failed\n  2  a usage error\n  3  no file\n", id="four recursive returns"),
        pytest.param(
            f"{CALLED}def k(h):\n{SITES}    return h()\n"
            "def main():\n    argparse.ArgumentParser().parse_args()\n    return k(two)\n",
            "  1  failed\n  2  a usage error\n  4  four\n",
            id="six recursive call sites passing a parameter",
        ),
        pytest.param(
            f"{CALLED}{CHAIN}def f20(a0, a1):\n    return a0()\n"
            "def main():\n    argparse.ArgumentParser().parse_args()\n    return f0(two, two)\n",
            "  1  failed\n  2  a usage error\n  4  four\n",
            id="twenty functions passing two parameters on",
        ),
    ],
)
def test_the_reader_reads_recursion_and_long_argument_chains_in_bounded_steps(body: str, table: str) -> None:
    # A key that told scopes apart by the whole chain of scopes their arguments came from met a new key at each
    # recursive call site, so the first two raised RecursionError after hundreds of steps, and building that key anew
    # for each scope walked the third's chain of arguments, about 21 million times without a key kept per scope.
    steps = 0
    counted: dict[str, Any] = {}
    for name in ("returns", "value", "keyed"):
        original = getattr(exit_tables.CodeReader, name)

        def step(self: exit_tables.CodeReader, *arguments: object, original: Any = original) -> Any:
            nonlocal steps
            steps += 1
            if steps > 500:
                raise AssertionError("the exit-table reader took more than 500 steps")
            return original(self, *arguments)

        counted[name] = step
    entry = 'if __name__ == "__main__":\n    run_main(main)\n'
    with mock.patch.multiple(exit_tables.CodeReader, **counted):
        found = problems({"tools/tool.py": script(f"A tool.\n\nExit status:\n{table}", body + entry)})
    assert found == []


ENTRY = 'if __name__ == "__main__":\n    run_main(main)\n'
PING_PONG = (
    "from pathlib import Path\n"
    "def ping(path: Path, depth: int) -> int:\n    if depth > 4:\n        return 3\n"
    "    if path.is_dir():\n        return pong(path / '__main__.py', depth + 1)\n"
    "    return pong(path.parent, depth + 1)\n"
    "def pong(path: Path, depth: int) -> int:\n"
    "    if path.is_symlink():\n        return ping(path.resolve(), depth + 1)\n"
    "    return ping(path.with_suffix('.py'), depth + 1)\n"
    "def main() -> int:\n    arguments = argparse.ArgumentParser()\n    arguments.add_argument('path')\n"
    "    return ping(Path(arguments.parse_args().path), 0)\n"
)


def reader_of(body: str) -> exit_tables.CodeReader:
    """The reader of a tool.py that imports run_main and argparse, holds body, and runs main, having read it."""
    source = f"{HEADER}{body}{ENTRY}"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "tool.py"
        path.write_text(source, encoding="utf-8")
        return exit_tables.read_codes(root, exit_tables.EntryPoint("tool.py", path, ast.parse(source)))


@pytest.mark.parametrize(
    "leaf",
    [pytest.param("    return p\n", id="returned"), pytest.param("    q = p\n    return q\n", id="bound to a local")],
)
def test_a_function_given_the_same_expressions_in_different_scopes_is_read_in_each(leaf: str) -> None:
    # leaf's p is given mid's x, the same expression in mid(3)'s scope and in mid(4)'s. A reader that keyed a scope by
    # its arguments' expressions alone read leaf, and its local q, once, from mid(3), and lost exit 4.
    body = (
        f"def leaf(p):\n{leaf}def mid(x):\n    return leaf(x)\n"
        "def main(flag):\n    argparse.ArgumentParser().parse_args()\n    return mid(3) if flag else mid(4)\n"
        f"{ENTRY}"
    )
    table = "Exit status:\n  1  failed\n  2  a usage error\n  3  three\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body)}) == [
        'tools/tool.py can exit 4, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
    ]
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}  4  four\n", body)}) == []


def test_a_recursion_that_passes_its_parameters_round_is_read_in_each_turn() -> None:
    # Each call of k passes its three parameters on, each to the next one's place. In every scope after the first,
    # k's arguments are the same three names, given in a scope of k, so a key by those expressions, or by them and a
    # cut in place of the scope they were given in, read two scopes and lost three(), exit 5. Followed through the
    # parameters that pass them on, they are one, two, and three, given in main, in each order.
    body = (
        f"{CALLED}def three():\n    return 5\n"
        "def k(x, y, z):\n    if c():\n        return x()\n    return k(y, z, x)\n"
        f"def main():\n    argparse.ArgumentParser().parse_args()\n    return k(one, two, three)\n{ENTRY}"
    )
    table = "Exit status:\n  1  failed\n  2  a usage error\n  3  three\n  4  four\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body)}) == [
        'tools/tool.py can exit 5, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
    ]
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}  5  five\n", body)}) == []


@pytest.mark.parametrize(
    ("body", "keys", "keyings"),
    [
        # main's scope, check's as main calls it, and one for each of check's four recursive call sites, whichever
        # scope of check it is made in.
        pytest.param(WALK, 6, 130, id="four recursive call sites"),
        # main's, two's, and each of f0 to f20's.
        pytest.param(
            f"{CALLED}{CHAIN}def f20(a0, a1):\n    return a0()\n"
            "def main():\n    argparse.ArgumentParser().parse_args()\n    return f0(two, two)\n",
            23,
            260,
            id="twenty functions passing two parameters on",
        ),
        # main's, ping's as main calls it, and pong's from that scope at each of ping's two call sites; then, past
        # one round, one for each of the four call sites, whichever scope it is made in.
        pytest.param(PING_PONG, 8, 130, id="two functions recursing into each other"),
    ],
)
def test_the_scope_keys_grow_with_the_functions_and_call_sites_and_each_scope_is_keyed_once(
    body: str, keys: int, keyings: int
) -> None:
    # A key over the whole chain of scopes the arguments were given in met a new key at each recursive call site in
    # each order, 29,462 of them for the first before RecursionError; built anew for each scope, it walked the
    # second's chain of two parameters twice at each of its twenty functions, about a million keyings. keyings is about
    # twice what the reader takes here: it keys each scope it reads in, and keys a new scope from the keys of the scope
    # each argument was given in and the scope its value was, each of which it keyed once before.
    calls = 0
    original = exit_tables.CodeReader.keyed

    def keyed(self: exit_tables.CodeReader, scope: exit_tables.Scope) -> exit_tables.Keyed:
        nonlocal calls
        calls += 1
        if calls > keyings or len(self.contents) > keys:
            raise AssertionError(f"the reader keyed {calls} times, into {len(self.contents)} keys")
        return original(self, scope)

    with mock.patch.object(exit_tables.CodeReader, "keyed", keyed):
        reader = reader_of(body)
    assert len(reader.contents) == keys


def test_a_function_two_arguments_pass_on_from_one_scope_is_looked_up_there_once() -> None:
    # Each fi passes both its parameters on to f(i+1) inside two tables, so the function f20 calls is reached
    # through 2**20 paths of tables. callables reads each table once for the question, in each scope, so it takes
    # four reads for each function; a set of reads begun anew in each scope it went out to took each path.
    chain = "".join(
        f"def f{index}(a, b):\n    return f{index + 1}({{'x': a, 'y': b}}['x'], {{'x': a, 'y': b}}['y'])\n"
        for index in range(20)
    )
    body = (
        f"def one():\n    return 3\ndef two():\n    return 4\n{chain}def f20(a, b):\n    return a()\n"
        "def main():\n    return f0(one, two)\n"
    )
    calls = 0
    original = exit_tables.CodeReader.callables

    def callables(self: exit_tables.CodeReader, *arguments: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 500:
            raise AssertionError("callables was called more than 500 times")
        return original(self, *arguments)

    with mock.patch.object(exit_tables.CodeReader, "callables", callables):
        reader = reader_of(body)
    assert reader.reading.codes == {1, 3, 4}


@pytest.mark.parametrize(
    ("body", "codes", "unread"),
    [
        # A table of functions that names itself leads back to its own first read, in the module's one scope or the
        # function's.
        pytest.param(
            "def one():\n    return 3\nTABLE = {'a': one}\nTABLE = {'b': TABLE['a'], 'a': one}\n"
            "def main():\n    return TABLE['b']()\n",
            {1, 3},
            [],
            id="a module's table that names itself",
        ),
        pytest.param(
            "def one():\n    return 3\n"
            "def main():\n    handler = {'a': one}\n    handler = {'b': handler['a']}\n    return handler['b']()\n",
            {1, 3},
            [],
            id="a local table that names itself",
        ),
        # Each value bound to a name is read: one bound to another name too, read first through that name, and one
        # after a value the reader cannot read for the element it is asked for.
        pytest.param(
            "def main(flag):\n    b = 4\n    a = b = 3\n    return a if flag else b\n",
            {1, 3, 4},
            [],
            id="a value bound to two names",
        ),
        pytest.param(
            "def pair():\n    return 5, 6\n"
            "def inner(flag):\n    a = (3, 4)\n    a, b = pair()\n    return a\n"
            "def main():\n    code, _ = inner(1)\n    return code\n",
            {1, 3},
            ["a"],
            id="a value after one bound to a tuple's item",
        ),
        # The item a value is read for: the one asked for when a name is bound to the whole value, and the name's own
        # when it is one of a tuple's targets.
        pytest.param(
            "def pair():\n    return 3, 4\ndef inner():\n    result = pair()\n    return result\n"
            "def main(flag):\n    a, b = inner()\n    return a if flag else b\n",
            {1, 3, 4},
            [],
            id="a local returned as a tuple",
        ),
        pytest.param(
            "def pair():\n    return 3, 4\ndef main(flag):\n    a, b = pair()\n    return a if flag else b\n",
            {1, 3, 4},
            [],
            id="a tuple's two targets",
        ),
    ],
)
def test_the_reader_reads_each_value_of_a_name_once_for_each_item(
    body: str, codes: set[int], unread: list[str]
) -> None:
    reading = reader_of(body).reading
    assert reading.codes == codes
    assert [expression for _, _, expression in reading.unread] == unread


def test_an_entry_point_without_a_table_fails_and_a_module_that_runs_no_runner_is_none() -> None:
    found = problems({"tools/tool.py": script("A tool.\n"), "tools/library.py": '"""A library."""\nVALUE = 1\n'})
    assert len(found) == 1
    assert found[0].startswith("tools/tool.py has no `Exit status:` table")


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("Exit status:\n  0  done\n  2  usage\n", "tools/tool.py can exit 1, which its exit table does not name"),
        (
            "Exit status:\n  0  done\n  1  failed\n  2  usage\n  3  never\n",
            "tools/tool.py names exit 3 in its exit table, which no path of its code exits with",
        ),
        ("Exit status:\n  1  failed\n  0  done\n  2  usage\n", "tools/tool.py names exit 0 after exit 1"),
        ("Exit status:\n  0  done\n  1  failed\n   2 usage\n", "tools/tool.py holds the line '2 usage'"),
        ("Exit status:\n\n", "tools/tool.py has a `Exit status:` heading with no row below it"),
        (f"{TABLE}\n{TABLE}", "tools/tool.py has 2 `Exit status:` tables"),
    ],
)
def test_a_table_out_of_form_or_that_misnames_a_code_fails(table: str, expected: str) -> None:
    found = problems({"tools/tool.py": script(f"A tool.\n\n{table}")})
    assert len(found) == 1, found
    assert found[0].startswith(expected), found


def test_a_row_continues_on_lines_indented_further_and_130_may_be_named() -> None:
    table = "Exit status:\n  0    done, every\n       part of it\n  1    failed\n  2    usage\n  130  cancelled\n"
    assert exit_tables.exit_table(table) == {0: "done, every part of it", 1: "failed", 2: "usage", 130: "cancelled"}
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}")}) == []


@pytest.mark.parametrize(
    ("body", "codes"),
    [
        ("def main() -> int:\n    return 3 if X else 4\n", {1, 3, 4}),
        ("STOPPED = 5\ndef main() -> int:\n    return STOPPED\n", {1, 5}),
        ("def main() -> int:\n    code, lines = run()\n    return code\ndef run():\n    return 6, []\n", {1, 6}),
        ("def main() -> int:\n    return wrap(lambda: 7)\ndef wrap(f):\n    return f()\n", None),
        (
            "def main() -> int:\n    return wrap(work)\ndef wrap(f):\n    return f()\ndef work():\n    return 8\n",
            {1, 8},
        ),
        ("def main():\n    print('x')\n", {0, 1}),
        # A function that ends in any exit call never falls through to return None, quit included.
        ("def main():\n    print('x')\n    quit(4)\n", {1, 4}),
        ("import sys\ndef main():\n    print('x')\n    sys.exit(4)\n", {1, 4}),
        # os._exit takes its code as status=, and a method of an exit call's name is no exit call.
        ("import os\ndef main():\n    print('x')\n    os._exit(status=4)\n", {1, 4}),
        ("def main():\n    print('x')\n    smtp.quit()\n", {0, 1}),
        ("import sys\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    sys.exit(9)\n", {0, 1, 9}),
        ("def main() -> int:\n    helper()\n    return 0\ndef helper():\n    raise SystemExit(15)\n", {0, 1, 15}),
        ("import sys\ndef main() -> int:\n    helper()\n    return 5\ndef helper():\n    sys.exit()\n", {0, 1, 5}),
        ("def main() -> int:\n    helper()\n    return 5\ndef helper():\n    raise SystemExit(None)\n", {0, 1, 5}),
        ("def main() -> int:\n    helper()\n    return 5\ndef helper():\n    raise SystemExit\n", {0, 1, 5}),
        ("def main() -> int:\n    helper()\n    return 0\ndef helper():\n    raise SystemExit(code=19)\n", {0, 1, 19}),
        (
            "import builtins\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    builtins.exit(20)\n",
            {0, 1, 20},
        ),
        ("import os\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    os._exit(21)\n", {0, 1, 21}),
        ("import sys\ndef main() -> int:\n    helper()\n    return 5\ndef helper():\n    sys.exit('no')\n", {1, 5}),
        (
            "import sys\nLATE = 18\ndef main() -> int:\n    helper()\n    return 5\ndef helper():\n"
            "    code = LATE if X else find()\n    sys.exit(code)\ndef find():\n    return 17\n",
            {1, 5, 17, 18},
        ),
        (
            "TABLE = {'a': first}\ndef main() -> int:\n    return TABLE.get(name)(x)\ndef first(x):\n    return 16\n",
            {1, 16},
        ),
        (
            "def main() -> int:\n    table = {'a': first, 'b': second}\n    return table[name](x)\n"
            "def first(x):\n    return 10\ndef second(x):\n    return 11\n",
            {1, 10, 11},
        ),
        (
            "class Result:\n    @property\n    def exit_code(self) -> int:\n        return 12\n"
            "def main() -> int:\n    return Result().exit_code\n",
            {1, 12},
        ),
        (
            "class Refused(Exception):\n    def __init__(self, exit_code=13):\n        self.exit_code = exit_code\n"
            "def main() -> int:\n    try:\n        check()\n    except Refused as error:\n"
            "        return error.exit_code\n"
            "    return 0\ndef check():\n    raise Refused(exit_code=14)\n",
            {0, 1, 13, 14},
        ),
    ],
)
def test_the_reader_follows_returns_through_conditionals_constants_locals_calls_tables_and_errors(
    body: str, codes: set[int] | None
) -> None:
    source = f"from console import run_main\n{body}if __name__ == '__main__':\n    run_main(main)\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "tool.py"
        path.write_text(source, encoding="utf-8")
        reader = exit_tables.read_codes(root, exit_tables.EntryPoint("tool.py", path, ast.parse(source)))
    if codes is None:
        assert reader.reading.unread, reader.reading
    else:
        assert reader.reading.codes == codes
        assert reader.reading.unread == []


PARSER_ERROR_TAIL = "    return 0\nif __name__ == '__main__':\n    run_main(main)\n"
# A parser bound in a class's body, and a class Tool whose property calls error on a receiver, with a main the reader
# reaches it from: the reader follows into a method through a property a return reads.
ANNOTATED = "    cli: argparse.ArgumentParser\n"


def tool_class(bound: str, receiver: str, parameter: str = "self") -> str:
    return (
        f"class Tool:\n{bound}    @property\n    def status({parameter}) -> int:\n        {receiver}.error('no')\n"
        "        return 0\ndef main() -> int:\n    if X:\n        return Tool().status\n"
    )


@pytest.mark.parametrize(
    ("body", "codes"),
    [
        # A parser's error exits 2 like a usage error, on every receiver read as a parser.
        ("def main() -> int:\n    check(argparse.ArgumentParser())\n", {0, 1, 2}),
        ("def main() -> int:\n    built = argparse.ArgumentParser()\n    built.error('no')\n", {0, 1, 2}),
        ("from argparse import ArgumentParser as P\ndef main() -> int:\n    b = P()\n    b.error('no')\n", {0, 1, 2}),
        (
            "def main() -> int:\n    commands = argparse.ArgumentParser().add_subparsers()\n"
            "    one = commands.add_parser('one')\n    one.error('no')\n",
            {0, 1, 2},
        ),
        (
            "def build() -> argparse.ArgumentParser:\n    return argparse.ArgumentParser()\n"
            "def main() -> int:\n    made = build()\n    made.error('no')\n",
            {0, 1, 2},
        ),
        ("def main() -> int:\n    made: 'argparse.ArgumentParser' = get()\n    made.error('no')\n", {0, 1, 2}),
        ("def main() -> int:\n    self.parser.error('no')\n", {0, 1, 2}),
        # A parser bound to an attribute chain is one on a receiver of that chain, and on no other.
        ("def main() -> int:\n    self.cli = argparse.ArgumentParser()\n    self.cli.error('no')\n", {0, 1, 2}),
        ("def main() -> int:\n    self.cli: argparse.ArgumentParser = get()\n    self.cli.error('no')\n", {0, 1, 2}),
        ("def main() -> int:\n    self.cli = argparse.ArgumentParser()\n    other.cli.error('no')\n", {0, 1}),
        ("def main() -> int:\n    self.cli = argparse.ArgumentParser()\n    self.log.error('no')\n", {0, 1}),
        ("def main() -> int:\n    cli = argparse.ArgumentParser()\n    self.cli.error('no')\n", {0, 1}),
        ("def main() -> int:\n    holder[0] = argparse.ArgumentParser()\n    made().error('no')\n", {0, 1}),
        # A name a class's body binds to a parser is one on its method's receiver, in that class's methods alone.
        (tool_class(ANNOTATED, "self.cli"), {0, 1, 2}),
        (tool_class(f"    if TYPE_CHECKING:\n    {ANNOTATED}", "self.cli"), {0, 1, 2}),
        (tool_class(f"    try:\n    {ANNOTATED}    except ImportError:\n        pass\n", "self.cli"), {0, 1, 2}),
        (tool_class(f"    with context():\n    {ANNOTATED}", "self.cli"), {0, 1, 2}),
        (tool_class("    cli = argparse.ArgumentParser()\n", "self.cli"), {0, 1, 2}),
        (tool_class(ANNOTATED, "this.cli", "this"), {0, 1, 2}),
        (tool_class(ANNOTATED, "self.log"), {0, 1}),
        (tool_class(ANNOTATED, "other.cli"), {0, 1}),
        (f"class Holder:\n{ANNOTATED}{tool_class('', 'self.cli')}", {0, 1}),
        # Every parsing call argparse offers exits 2 on a usage error.
        ("def main() -> int:\n    got.parse_known_intermixed_args()\n", {0, 1, 2}),
        # A quoted annotation that does not parse names no parser.
        ("def main() -> int:\n    made: 'not a (type' = get()\n    made.error('no')\n", {0, 1}),
        # An error method of anything else, a logger's, is no usage exit.
        ("import logging\ndef main() -> int:\n    log = logging.getLogger()\n    log.error('no')\n", {0, 1}),
        ("def main() -> int:\n    logging.error('no')\n", {0, 1}),
    ],
)
def test_a_parser_s_error_is_the_usage_exit_and_another_object_s_error_is_none(body: str, codes: set[int]) -> None:
    source = f"from console import run_main\nimport argparse\n{body}{PARSER_ERROR_TAIL}"
    if "check(" in body:
        source += "def check(given: argparse.ArgumentParser) -> None:\n    given.error('no')\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "tool.py"
        path.write_text(source, encoding="utf-8")
        reader = exit_tables.read_codes(root, exit_tables.EntryPoint("tool.py", path, ast.parse(source)))
    assert reader.reading.codes == codes
    assert reader.reading.unread == []


@pytest.mark.parametrize(
    ("imports", "static", "made"),
    [
        ("", "staticmethod", "classmethod"),
        ("import builtins\n", "builtins.staticmethod", "builtins.classmethod"),
        ("from builtins import classmethod, staticmethod\n", "staticmethod", "classmethod"),
        ("import builtins as b\n", "b.staticmethod", "b.classmethod"),
    ],
)
def test_a_class_s_parser_is_reached_on_each_receiver_its_own_methods_take_and_on_no_other(
    imports: str, static: str, made: str
) -> None:
    source = (
        f"import argparse\n{imports}"
        "class Outer:\n    cli: argparse.ArgumentParser\n"
        f"    def first(this, /): pass\n    @{made}\n    def made(cls): pass\n"
        f"    @{static}\n    def static(given): pass\n    def bare(): pass\n"
        "    if X:\n        def guarded(self): pass\n"
        "    class Inner:\n        def method(self): pass\n"
    )
    tree = ast.parse(source)
    found = {method.name: chains for method, chains in exit_tables.method_parsers(tree, import_aliases(tree)).items()}
    assert found == {"first": {"this.cli"}, "made": {"cls.cli"}, "guarded": {"self.cli"}, "method": set()}


def test_an_entry_point_whose_only_usage_exit_is_parser_error_names_2() -> None:
    body = (
        "def main() -> int:\n    parser = argparse.ArgumentParser()\n    if X:\n        parser.error('no')\n"
        "    return 0\n"
        'if __name__ == "__main__":\n    run_main(main)\n'
    )
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)}) == []
    found = problems({"tools/tool.py": script("A tool.\n\nExit status:\n  0  done\n  1  failed\n", body)})
    assert len(found) == 1, found
    assert found[0].startswith("tools/tool.py can exit 2, which its exit table does not name"), found


def test_an_entry_point_whose_only_usage_exit_is_an_attribute_parser_s_error_names_2() -> None:
    body = (
        "def main() -> int:\n    self.cli = argparse.ArgumentParser()\n    if X:\n        self.cli.error('no')\n"
        "    return 0\n"
        'if __name__ == "__main__":\n    run_main(main)\n'
    )
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)}) == []
    found = problems({"tools/tool.py": script("A tool.\n\nExit status:\n  0  done\n  1  failed\n", body)})
    assert len(found) == 1, found
    assert found[0].startswith("tools/tool.py can exit 2, which its exit table does not name"), found


def test_the_reader_follows_an_import_into_a_module_beside_the_entry_point() -> None:
    body = "from helpers import run\ndef main() -> int:\n    return run()\n"
    body += "if __name__ == '__main__':\n    run_main(main)\n"
    found = problems(
        {
            "tools/tool.py": script("A tool.\n\nExit status:\n  1  failed\n  4  from the helper\n", body, HEADER),
            "tools/helpers.py": "FOUR = 4\ndef run() -> int:\n    return FOUR\n",
        }
    )
    assert found == []


def test_an_unread_return_fails_until_an_allowance_names_it_and_an_allowance_naming_nothing_fails() -> None:
    body = "def main() -> int:\n    return thing.code\nif __name__ == '__main__':\n    run_main(main)\n"
    unread = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)})
    assert len(unread) == 1
    assert unread[0].startswith(
        "tools/tool.py:11 returns or exits with thing.code, an exit code the exit-table reader cannot"
    )
    allowed = "EXIT_CODES_ALLOWED = {'thing.code': 'the thing says 0 or 2'}\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", allowed + body)}) == []
    stale = "EXIT_CODES_ALLOWED = {'other.code': 'nothing returns it'}\n"
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", stale + MAIN)})
    assert found == [
        "tools/tool.py names 'other.code' in EXIT_CODES_ALLOWED, which is no return the exit-table reader cannot "
        "follow from an entry point; remove it"
    ]


def test_an_exit_call_with_a_code_the_reader_cannot_follow_fails_until_an_allowance_names_it() -> None:
    body = (
        "import sys\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    sys.exit(thing.code)\n"
        "if __name__ == '__main__':\n    run_main(main)\n"
    )
    unread = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)})
    assert len(unread) == 1, unread
    assert unread[0].startswith("tools/tool.py:15 returns or exits with thing.code, an exit code the exit-table reader")
    allowed = "EXIT_CODES_ALLOWED = {'thing.code': 'the thing exits 0 or 2'}\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", allowed + body)}) == []


@pytest.mark.parametrize(
    "allowance",
    [
        "EXIT_CODES_ALLOWED = {'thing.code': ''}\n",
        "EXIT_CODES_ALLOWED = {'thing.code': '   '}\n",
        "EXIT_CODES_ALLOWED = {'thing.code': 3}\n",
    ],
)
def test_an_allowance_without_a_non_empty_reason_fails(allowance: str) -> None:
    body = "def main() -> int:\n    return thing.code\nif __name__ == '__main__':\n    run_main(main)\n"
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", allowance + body)})
    assert (
        "tools/tool.py: EXIT_CODES_ALLOWED must map each expression to where its codes come from, a non-empty reason"
        in found
    ), found


NO_ENTRY_POINT = (
    "the exit-table policy found no entry point, no module whose __main__ block calls console.run_main, so it read "
    "no exit table: the scan or the runner it looks for has moved"
)


def test_suites_and_fixtures_are_no_entry_points_and_a_run_that_finds_none_fails() -> None:
    entry = script("A suite.\n")
    assert problems({"tools/test_tool.py": entry, "tests/fixtures/tool.py": entry}) == [NO_ENTRY_POINT]
    assert problems({}) == [NO_ENTRY_POINT]


def test_an_allowance_in_a_module_no_entry_point_reaches_fails_when_it_names_no_return() -> None:
    found = problems(
        {
            "tools/tool.py": script(f"A tool.\n\n{TABLE}"),
            "tools/unreached.py": "EXIT_CODES_ALLOWED = {'gone.code': 'its caller was removed'}\n",
        }
    )
    assert found == [
        "tools/unreached.py names 'gone.code' in EXIT_CODES_ALLOWED, which is no return the exit-table reader cannot "
        "follow from an entry point; remove it"
    ]


def test_an_allowed_return_lets_the_table_name_a_code_the_reader_cannot_see() -> None:
    # thing.code may be 3, which no path the reader follows exits with; the allowance's reason says where it comes from.
    body = (
        "EXIT_CODES_ALLOWED = {'thing.code': 'the thing exits 0 or 3'}\n"
        "def main() -> int:\n    if flag:\n        return thing.code\n    return 0\n"
        "if __name__ == '__main__':\n    run_main(main)\n"
    )
    table = "Exit status:\n  0  done\n  1  a last line FAILED <reason>\n  3  what the thing says\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body, "from console import run_main\n")}) == []


def test_a_run_main_lambda_target_is_read_as_the_call_it_makes() -> None:
    body = (
        "import sys\ndef main(arguments: list[str]) -> int:\n    return 4 if arguments else 0\n"
        "if __name__ == '__main__':\n    run_main(lambda: main(sys.argv[1:]))\n"
    )
    table = "Exit status:\n  0  done\n  1  a last line FAILED <reason>\n  4  given arguments\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body, "from console import run_main\n")}) == []
    short = "Exit status:\n  0  done\n  1  a last line FAILED <reason>\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{short}", body, "from console import run_main\n")}) == [
        'tools/tool.py can exit 4, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
    ]

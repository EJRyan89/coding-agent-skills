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


# The steps a test's reader may take, together: each read it queues, function it looks up, module scope it asks for,
# and pass it starts after a late caller; each time callables' walk asks whether a value is answered; and each time
# _functions reads whether a group's member is told. Each loop the reader runs takes one of these on every turn, so a
# reader that loops fails within a second instead of running to the suite's time limit. The limit is past eight times
# what the deepest fixture here takes, the ten functions calling each other at 46,641, most of them the walk's and
# _functions' steps for each value they read.
STEP_LIMIT = 400_000


@pytest.fixture(autouse=True)
def bounded_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a test whose exit-table reader takes more than STEP_LIMIT steps: reads queued, functions looked up, module
    scopes asked for, passes started after a late caller, answers looked up, and members' told read."""
    steps = 0

    def step() -> None:
        nonlocal steps
        steps += 1
        if steps > STEP_LIMIT:
            raise AssertionError(f"the exit-table reader took more than {STEP_LIMIT} steps")

    for name in ("later", "function_named", "module_scope", "again"):
        original = getattr(exit_tables.CodeReader, name)

        def counted(self: exit_tables.CodeReader, *arguments: Any, original: Any = original) -> Any:
            step()
            return original(self, *arguments)

        monkeypatch.setattr(exit_tables.CodeReader, name, counted)

    class Answers(dict[Any, Any]):
        def __contains__(self, key: object) -> bool:
            step()
            return super().__contains__(key)

    class Told(dict[Any, bool]):
        def __getitem__(self, key: Any) -> bool:
            step()
            return super().__getitem__(key)

    made = exit_tables.CodeReader.__init__

    def answering(self: exit_tables.CodeReader, *arguments: Any) -> None:
        made(self, *arguments)
        self.answers = Answers()

    functions = exit_tables._functions

    def bounded(parts: Any, values: Any, readers: Any, outside: Any, told: Any) -> Any:
        return functions(parts, values, readers, outside, Told(told))

    monkeypatch.setattr(exit_tables.CodeReader, "__init__", answering)
    monkeypatch.setattr(exit_tables, "_functions", bounded)


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
    for name in ("returns", "value", "parameter"):
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
# One function of four parameters with six recursive calls, each passing them on rotated, a number in one place.
ROTATIONS = (
    "def c():\n    return bool(argparse)\ndef k(p0, p1, p2, p3):\n    if c():\n        return p0\n"
    + "".join(
        f"    if c():\n        return k({', '.join(rotated)})\n"
        for site in range(6)
        for names in [[f"p{index}" for index in range(4)]]
        for rotated in [
            [
                str(10 + site) if place == site % 4 else name
                for place, name in enumerate(names[site % 4 + 1 :] + names[: site % 4 + 1])
            ]
        ]
    )
    + "    return p0\ndef main():\n    return k(3, 4, 5, 6)\n"
)
# Four functions of three parameters that call each other and themselves, by position and by keyword.
CALL_EACH_OTHER = (
    "def c():\n    return bool(argparse)\n"
    "def f(a, b, d):\n    if c(): return g(d, d, a) if c() else k(five, b, one)\n"
    "    if c(): return k(a, d, d) if c() else m(d=b, a=b, b=d)\n"
    "def g(a, b, d):\n    if c(): return m(two, a, a)\n"
    "def k(a, b, d):\n    if c(): return f(a, b, d)\n    if c(): return k(b, a, three)\n"
    "    if c(): return k(b, two, b) if c() else k(b=five, a=a, d=three)\n"
    "    r3 = k(a, four, b)\n    if c(): return r3\n"
    "def m(a, b, d):\n    r0 = f(b=two, d=b, a=a)\n    if c(): return r0\n    if c(): return f(d, four, two)\n"
    "    if c(): return f(a=d, b=b, d=a)\n"
    "def main():\n    return m(one, five, three)\n"
)
# Twelve functions, each calling the next with one of two numbers, by position and by keyword in a conditional.
LADDER = "".join(
    f"def f{index}(p):\n    if c():\n        return f{index + 1}(1)\n    return f{index + 1}(2)\n"
    for index in range(12)
)
KEYWORD_LADDER = "".join(
    f"def f{index}(p):\n    return f{index + 1}(p=1) if c() else f{index + 1}(p=2)\n" for index in range(12)
)
LADDER_END = "def c():\n    return bool(argparse)\ndef f12(p):\n    return p\ndef main():\n    return f0(0)\n"
# Ten calls through a table of ten functions, each call able to name any of them.
TABLE_CALLS = (
    "def c():\n    return bool(argparse)\n"
    + "".join(f"def h{index}(x):\n    return x if c() else {index + 3}\n" for index in range(10))
    + "T = {"
    + ", ".join(f"'{index}': h{index}" for index in range(10))
    + "}\ndef main():\n    k = input()\n"
    + "".join(f"    if c():\n        return T[k]({index + 3})\n" for index in range(10))
    + "    return 0\n"
)


def relays(count: int, returning: bool = False) -> str:
    """A ladder of relays: R hands each Li on through F, which calls it with v, and Li hands on L(i+1) or returns i + 3,
    and, when returning, v too, so each relay's caller is found one pass after the last. Its codes are 3, which main
    gives v, and each i + 3."""
    value = "v if c() else " if returning else ""
    return (
        "def c():\n    return bool(argparse)\ndef F(h, v):\n    return h(v)\ndef R(h, v):\n    return F(h, v)\n"
        + "".join(
            f"def L{index}(v):\n    return R(L{index + 1}, v) if c() else {value}{index + 3}\n"
            for index in range(1, count)
        )
        + f"def L{count}(v):\n    return v\ndef main():\n    return R(L1, 3)\n"
    )


def mutual_recursion(count: int) -> str:
    """Functions of four parameters that call each other at six sites each, passing their parameters on in turn,
    rotated and reversed, and return one of them called; main gives them four functions returning 3 to 6."""
    names = ["a", "b", "d", "e"]
    body = "def c():\n    return bool(argparse)\n" + "".join(
        f"def leaf{index}():\n    return {index + 3}\n" for index in range(4)
    )
    for index in range(count):
        body += f"def r{index}(a, b, d, e):\n"
        for site in range(6):
            turned = names[site % 4 :] + names[: site % 4]
            passed = turned[::-1] if site % 2 else turned
            body += f"    if c():\n        return r{(index * 7 + site * 3 + 1) % count}({', '.join(passed)})\n"
        body += f"    return {names[index % 4]}()\n"
    return body + "def main():\n    return r0(leaf0, leaf1, leaf2, leaf3)\n"


# Five relays.
RELAYS = relays(5)


def reader_of(body: str) -> exit_tables.CodeReader:
    """The reader of a tool.py that imports run_main and argparse, holds body, and runs main, having read it."""
    source = f"{HEADER}{body}{ENTRY}"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "tool.py"
        path.write_text(source, encoding="utf-8")
        return exit_tables.read_codes(root, exit_tables.EntryPoint("tool.py", path, ast.parse(source)))


RETURNED = "    return mid(3) if flag else mid(4)\n"


@pytest.mark.parametrize(
    ("leaf", "mid", "start"),
    [
        pytest.param("    return p\n", "    return leaf(x)\n", RETURNED, id="returned"),
        pytest.param("    q = p\n    return q\n", "    return leaf(x)\n", RETURNED, id="bound to a local in leaf"),
        pytest.param("    return p\n", "    y = x\n    return leaf(y)\n", RETURNED, id="bound to a local in mid"),
    ],
)
def test_a_function_given_the_same_expressions_in_different_scopes_is_read_in_each(
    leaf: str, mid: str, start: str
) -> None:
    # leaf's p is given the same expression in mid(3)'s scope and in mid(4)'s. A reader that read a scope only as the
    # first call to reach it gave it, by its arguments' expressions, read leaf, and its local q, once, from mid(3), and
    # lost exit 4.
    body = (
        f"def leaf(p):\n{leaf}def mid(x):\n{mid}"
        f"def main(flag):\n    argparse.ArgumentParser().parse_args()\n{start}{ENTRY}"
    )
    table = "Exit status:\n  1  failed\n  2  a usage error\n  3  three\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}", body)}) == [
        'tools/tool.py can exit 4, which its exit table does not name; see "Script results" in docs/adding-a-skill.md'
    ]
    assert problems({"tools/tool.py": script(f"A tool.\n\n{table}  4  four\n", body)}) == []


def test_a_recursion_that_passes_its_parameters_round_is_read_in_each_turn() -> None:
    # Each call of k passes its three parameters on, each to the next one's place, so after the first call k's
    # arguments are the same three names, given in a scope of k. Read as every value the scopes that called it give
    # them, they are one, two, and three, given in main; read only as the first call gave them, three(), exit 5, was
    # lost.
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
    ("body", "scopes", "passes", "codes"),
    [
        pytest.param(WALK, 7, 1, {0, 1, 2, 3}, id="four recursive call sites"),
        pytest.param(
            f"{CALLED}{CHAIN}def f20(a0, a1):\n    return a0()\n"
            "def main():\n    argparse.ArgumentParser().parse_args()\n    return f0(two, two)\n",
            24,
            1,
            {1, 2, 4},
            id="twenty functions passing two parameters on",
        ),
        pytest.param(PING_PONG, 7, 1, {1, 2, 3}, id="two functions recursing into each other"),
        pytest.param(ROTATIONS, 9, 2, {1, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15}, id="six calls rotating four parameters"),
        pytest.param(CALL_EACH_OTHER, 16, 1, {0, 1}, id="four functions calling each other"),
        pytest.param(LADDER + LADDER_END, 27, 2, {1, 2}, id="a ladder of numbers"),
        pytest.param(KEYWORD_LADDER + LADDER_END, 27, 2, {1, 2}, id="a ladder of keywords in conditionals"),
        pytest.param(TABLE_CALLS, 102, 1, {0, 1, *range(3, 13)}, id="ten calls through a table of ten functions"),
        pytest.param(RELAYS, 13, 5, {1, 3, 4, 5, 6, 7}, id="five relays"),
    ],
)
def test_the_reader_keeps_a_scope_for_each_function_a_call_site_names_and_reads_a_pass_per_late_caller(
    body: str, scopes: int, passes: int, codes: set[int]
) -> None:
    # The reader keeps no more scopes than the source holds call sites, times the functions each can name, plus one for
    # each function it runs with no arguments and one for its module's own body (none of these reads a call for an
    # item), however its calls recurse and whatever values reach them. A key
    # that followed each argument's value and the scope it was given in multiplied each function's keys by the values
    # reaching each parameter, and doubled them at each step of a ladder: 1,953 for the rotations, 45,535 for the four
    # functions calling each other, both past Python's recursion limit, and 4,096 for each twelve-step ladder. A pass
    # follows only one in which a caller reached a scope after a read asked its callers, so a shape with no such caller
    # reads in one, and each relay found late takes one more. The reader takes at most 555 steps on these, so a reader
    # that loops fails at 1,000.
    tree = ast.parse(f"{HEADER}{body}")
    functions = sum(isinstance(node, ast.FunctionDef) for node in ast.walk(tree))
    bound = sum(isinstance(node, ast.Call) for node in ast.walk(tree)) * functions + functions + 1
    steps = 0
    counted: dict[str, Any] = {}
    for name in ("returns", "value", "function_named"):
        original = getattr(exit_tables.CodeReader, name)

        def step(self: exit_tables.CodeReader, *arguments: object, original: Any = original) -> Any:
            nonlocal steps
            steps += 1
            if len(self.scopes) > bound or steps > 1000:
                raise AssertionError(f"the reader kept {len(self.scopes)} scopes, past {bound}, or looped")
            return original(self, *arguments)

        counted[name] = step
    with mock.patch.multiple(exit_tables.CodeReader, **counted):
        reader = reader_of(body)
    assert reader.reading.codes == codes
    assert len(reader.scopes) == scopes
    assert reader.passes == passes


@pytest.mark.parametrize(
    ("body", "per_node", "passes", "codes"),
    [
        pytest.param(relays(100, returning=True), 2, 100, {1, *range(3, 103)}, id="a hundred relays"),
        pytest.param(mutual_recursion(10), 25, 2, {1, 3, 4, 5, 6}, id="ten functions calling each other"),
    ],
)
def test_a_late_pass_reads_only_what_its_late_callers_reach_and_each_called_expression_is_answered_once(
    body: str, per_node: int, passes: int, codes: set[int]
) -> None:
    # A step is a read queued or run, a function looked up, or a called expression's parts read, with each value they
    # hold. A pass that read the whole entry point again read each relay once for every relay found before it, so a
    # ladder of k relays took steps quadratic in k; and a walk that answered each call through a parameter anew went
    # out to every scope that called it, and on to theirs, for each call, so functions calling each other took steps
    # quadratic in their call sites. For each node of the syntax tree, the reader takes 1.4 steps on the relays and 21
    # on the functions calling each other, at any size, so it fails past 2 and 25; the quadratic readers took 73 and
    # 409 here, and 219 and 1,240 at three times the size. A reader that read a parameter's callers, followed a call's
    # functions, or answered a parameter's callers again from the first, rather than only those new since, took 2.5 to
    # 5.8 on the relays.
    tree = ast.parse(f"{HEADER}{body}{ENTRY}")
    bound = per_node * sum(1 for _ in ast.walk(tree))
    steps = 0
    counted: dict[str, Any] = {}
    for name in ("returns", "value", "later", "_parts", "function_named"):
        original = getattr(exit_tables.CodeReader, name)

        def step(self: exit_tables.CodeReader, *arguments: object, original: Any = original) -> Any:
            nonlocal steps
            result = original(self, *arguments)
            steps += 1 + (len(result.values) if isinstance(result, exit_tables.Parts) else 0)
            if steps > bound:
                raise AssertionError(f"the exit-table reader took more than {bound} steps")
            return result

        counted[name] = step
    with mock.patch.multiple(exit_tables.CodeReader, **counted):
        reader = reader_of(body)
    assert reader.reading.codes == codes
    assert reader.passes == passes


# Shapes in which a caller is found late, or a group of called expressions reaches its own answer: the reader reads
# each as a reader that read the whole entry point again each pass and answered each call afresh did. Each was found
# where reading again a parameter's new callers, a group dropped for a late caller and the groups that read it, a
# parameter's answer grown by its new callers, or the group answered with it, read a code, an unread return, or a
# scope that reader did not, or none it did, and cut to the lines that show it.
@pytest.mark.parametrize(
    ("body", "codes", "unread"),
    [
        pytest.param(
            (
                "def five(x):\n"
                "    return 6\n"
                "def f(h, v):\n"
                "    if c(): return m(v=3, h=h)\n"
                "def g(h, v):\n"
                "    if c(): return h(v)\n"
                "    return k(h, v)\n"
                "def k(h, v):\n"
                "    if c(): return f(h, v)\n"
                "def m(h, v):\n"
                "    q1 = h\n"
                "    return g(v=v, h=q1)\n"
                "def main():\n"
                "    return k(five, 8) if c() else g(ident, 3)\n"
            ),
            {0, 1, 6},
            ["h(v)"],
            id="a function handed on through a parameter and called with a value",
        ),
        pytest.param(
            (
                "def f(p, q):\n"
                "    b = p\n"
                "    b = a\n"
                "    if c(): return g(p, b)\n"
                "    return p()\n"
                "def g(p, q):\n"
                "    if c(): return f(q, p)\n"
                "def main():\n"
                "    return g(obj.attr, leaf2) if c() else f(leaf1, leaf1)\n"
            ),
            {0, 1},
            ["p()"],
            id="a table value read through a parameter",
        ),
        pytest.param(
            (
                "def f(h, n):\n"
                "    v = h\n"
                "    v = {'a': v}.get('a')\n"
                "    if c(): return v()\n"
                "def main():\n"
                "    return f(three, four) if c() else g(four, 7)\n"
            ),
            {0, 1},
            ["g(four, 7)", "v()"],
            id="a local bound to a parameter a recursion passes on",
        ),
        pytest.param(
            (
                "def R(h, v):\n"
                "    if c(): return R(v, h)\n"
                "    return v(v)\n"
                "def P(h, v):\n"
                "    return v(v)\n"
                "def L0(v):\n"
                "    return R(h=L2, v=8) if c() else L3(v)\n"
                "def L2(v):\n"
                "    return R(L3, 3) if c() else v\n"
                "def L3(v):\n"
                "    return P(L4, v) if c() else L3(v)\n"
                "def main():\n"
                "    return R(L0, 7)\n"
            ),
            {1, 3, 7, 8},
            ["L0", "L2", "L3", "v(v)", "v(v)"],
            id="relays found late through two functions",
        ),
        pytest.param(
            (
                "def R(h, v):\n"
                "    w0 = h\n"
                "    t1 = {'a': h, 'b': v}\n"
                "    if c(): return t1['a'](h)\n"
                "    p2, q2 = P(w0, h), 1\n"
                "    if c(): return p2\n"
                "def S(h, v):\n"
                "    t0 = {'a': h, 'b': L3}\n"
                "    if c(): return t0['a'](h)\n"
                "def P(h, v):\n"
                "    if c(): return S(h, 9)\n"
                "    return h(h)\n"
                "def L0(v):\n"
                "    return S(h=L2, v=v) if c() else L0(v)\n"
                "def L2(v):\n"
                "    return P(h=L3, v=v) if c() else L2(v)\n"
                "def L3(v):\n"
                "    return R(L4, v) if c() else L4(v)\n"
                "def L4(v):\n"
                "    return v if c() else 9\n"
                "def main():\n"
                "    return S(L0, 4)\n"
            ),
            {0, 1, 9},
            ["L0", "L2", "L3", "L4"],
            id="relays found late through tables and pairs",
        ),
        pytest.param(
            (
                "def F(h, v):\n"
                "    if c(): return h(L2)\n"
                "    if c(): return v(h)\n"
                "    return v(h)\n"
                "def R(h, v):\n"
                "    if c(): return F(v, h)\n"
                "    return h(h)\n"
                "def L0(v):\n"
                "    return R(L1, v) if c() else L1(v)\n"
                "def main():\n"
                "    return R(L0, 9)\n"
            ),
            {1},
            ["L1(v)", "h(L2)", "h(h)", "v(h)", "v(h)"],
            id="a parameter passed back as the function to call",
        ),
        pytest.param(
            (
                "def F(h, v):\n"
                "    return v(h)\n"
                "def R(h, v):\n"
                "    if c(): return F(v, h)\n"
                "    return h(h)\n"
                "def L0(v):\n"
                "    return R(L1, v) if c() else L1(v)\n"
                "def L1(v):\n"
                "    return F(h=L2, v=v) if c() else v\n"
                "def main():\n"
                "    return R(L0, 9)\n"
            ),
            {1, 9},
            ["L0", "L1", "L2", "v(h)"],
            id="a relay given its function by keyword",
        ),
        pytest.param(
            (
                "def f(h, n):\n"
                "    return k(h, h)\n"
                "def k(h, n):\n"
                "    v = h\n"
                "    w = v\n"
                "    if c(): return w()\n"
                "    return f(h, n)\n"
                "def main():\n"
                "    return f(four, 7)\n"
            ),
            {1},
            ["w()"],
            id="a local chain through a recursion's parameter",
        ),
        pytest.param(
            (
                "def one():\n"
                "    return 6\n"
                "def f(h, n):\n"
                "    return g(h, two)\n"
                "def g(h, n):\n"
                "    if c(): return k(h, n)\n"
                "def k(h, n):\n"
                "    pair = k(n, h)\n"
                "    if c(): return pair\n"
                "    if c(): return n()\n"
                "def main():\n"
                "    return g(one, 7) if c() else f(three, 7)\n"
            ),
            {0, 1, 6},
            ["n()"],
            id="a pair returned by a recursion that swaps its parameters",
        ),
        pytest.param(
            (
                "def three():\n"
                "    return 6\n"
                "def f(h, n):\n"
                "    if c(): return k(h, n) if c() else g(n, three)\n"
                "def g(h, n):\n"
                "    v = h\n"
                "    w = v\n"
                "    if c(): return w()\n"
                "    if c(): return f(four, three)\n"
                "def main():\n"
                "    return f(one, one)\n"
            ),
            {0, 1, 6},
            ["k(h, n)", "w()"],
            id="two locals through a parameter given late",
        ),
        pytest.param(
            (
                "def five(x):\n"
                "    return 5\n"
                "def f(h, v):\n"
                "    if c(): return m(v=3, h=h)\n"
                "    return h(v)\n"
                "def g(h, v):\n"
                "    if c(): return h(v)\n"
                "    return k(h, v)\n"
                "def k(h, v):\n"
                "    if c(): return f(h, v)\n"
                "def m(h, v):\n"
                "    if c(): return f(h, 7)\n"
                "    q1 = h\n"
                "    return g(v=v, h=q1)\n"
                "def main():\n"
                "    return k(five, 8) if c() else g(ident, 3)\n"
            ),
            {0, 1, 5},
            ["h(v)", "h(v)"],
            id="a function bound to a local and called with a new value",
        ),
        pytest.param(
            (
                "def four():\n"
                "    return 6\n"
                "def f(h, n):\n"
                "    return g(h, n)\n"
                "def g(h, n):\n"
                "    x = f\n"
                "    if c(): return x(n, n)\n"
                "    return h()\n"
                "def main():\n"
                "    return f(one, four) if c() else g(one, 7)\n"
            ),
            {1, 6},
            ["h()"],
            id="a function bound to a local in a recursion",
        ),
        pytest.param(
            (
                "def six(x):\n"
                "    return 6\n"
                "def f(h, v):\n"
                "    if c(): return h(v)\n"
                "    return g(h, 3)\n"
                "def g(h, v):\n"
                "    if c(): return g(h, v)\n"
                "    return m(v=v, h=h)\n"
                "def k(h, v):\n"
                "    if c(): return f(h, v)\n"
                "def m(h, v):\n"
                "    q0 = h\n"
                "    return q0(v)\n"
                "def main():\n"
                "    return k(ident, 4) if c() else k(six, 4)\n"
            ),
            {0, 1, 6},
            ["h(v)", "q0(v)"],
            id="a function handed on by keyword through a recursion",
        ),
        pytest.param(
            (
                "def f(h, n):\n"
                "    if c(): return n\n"
                "    return k(h, n)\n"
                "def g(h, n):\n"
                "    if c(): return k(one, n)\n"
                "def k(h, n):\n"
                "    if c(): return f(n, h)\n"
                "    return g(n, n)\n"
                "def main():\n"
                "    return g(one, two)\n"
            ),
            {0, 1},
            ["one", "two"],
            id="parameters swapped back and forth",
        ),
        pytest.param(
            (
                "def f(h, n):\n"
                "    if c(): return k(n, h)\n"
                "    return h()\n"
                "def k(h, n):\n"
                "    return f(h, h)\n"
                "def main():\n"
                "    return g(three, three) if c() else f(one, one)\n"
            ),
            {1},
            ["g(three, three)", "h()"],
            id="a call of an unknown function beside a recursion",
        ),
    ],
)
def test_late_callers_and_groups_of_called_expressions_read_what_a_whole_reading_each_pass_reads(
    body: str, codes: set[int], unread: list[str]
) -> None:
    reader = reader_of(body)
    assert reader.reading.codes == codes
    assert sorted(expression for _, _, expression in reader.reading.unread) == unread


def group_answers(
    members: dict[str, exit_tables.Parts], values: dict[str, list[str]], outside: dict[str, exit_tables.Answer]
) -> tuple[dict[str, bool], dict[str, list[str]]]:
    """Which members of a group, by name, are told, and each one's functions by name, given each member's parts, the
    members and outside expressions it leads to, and the outside expressions' answers."""
    keys = {name: (index, 0) for index, name in enumerate([*members, *outside])}
    readers: dict[exit_tables.Key, list[exit_tables.Key]] = {keys[name]: [] for name in members}
    for name in members:
        for child in values[name]:
            if child in members:
                readers[keys[child]].append(keys[name])
    parts = {keys[name]: made for name, made in members.items()}
    children = {keys[name]: [keys[child] for child in values[name]] for name in members}
    answers = {keys[name]: answer for name, answer in outside.items()}
    told = exit_tables._told(parts, children, readers, answers)
    functions = exit_tables._functions(parts, children, readers, answers, told)
    names = {key: name for name, key in keys.items()}
    return (
        {names[key]: each for key, each in told.items()},
        {names[key]: [function.name for _, function in found.values()] for key, found in functions.items()},
    )


def test_a_group_is_told_and_names_its_functions_as_its_values_do_however_they_lead_to_each_other() -> None:
    # A value the reader cannot tell makes a member that needs all its values untold, and a parameter untold once none
    # of its values is told; the untold spread through the group, a member reached again once untold read no further,
    # and a parameter still told by one value read past. The functions spread from member to told member until none
    # gains one, an untold member keeping none.
    module = exit_tables.Module(Path("tool.py"), ast.parse("def f():\n    return 3\n"))
    function = module.tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    group = exit_tables.Group([])
    untold = exit_tables.Answer(group, {}, told=False, complete=False)
    named = exit_tables.Answer(group, {id(function): (module, function)}, told=True, complete=True)
    plain, parameter = exit_tables.Parts(), exit_tables.Parts(callers=True)
    told, functions = group_answers(
        {"a": plain, "b": parameter, "h": parameter, "d": plain, "e": parameter, "g": plain},
        {"a": ["u"], "b": ["a"], "h": ["a", "f"], "d": ["a", "b"], "e": ["a", "b", "d"], "g": ["e", "f"]},
        {"u": untold, "f": named},
    )
    assert told == {"a": False, "b": False, "h": True, "d": False, "e": False, "g": False}
    assert functions == {"a": [], "b": [], "h": ["f"], "d": [], "e": [], "g": []}
    told, functions = group_answers(
        {"x": plain, "y": parameter, "z": parameter, "w": plain},
        {"x": ["u", "y"], "y": ["x", "z"], "z": ["y", "w"], "w": ["f", "z"]},
        {"u": untold, "f": named},
    )
    assert told == {"x": False, "y": True, "z": True, "w": True}
    assert functions == {"x": [], "y": ["f"], "z": ["f"], "w": ["f"]}


def test_a_call_through_a_table_keeps_a_scope_for_each_function_it_can_name() -> None:
    # Each of the ten calls through the table can name each of its ten functions, so the reader keeps a scope for each
    # pair: more than the calls the source holds plus its functions, and no more than the calls times the functions.
    tree = ast.parse(f"{HEADER}{TABLE_CALLS}")
    calls = sum(isinstance(node, ast.Call) for node in ast.walk(tree))
    functions = sum(isinstance(node, ast.FunctionDef) for node in ast.walk(tree))
    scopes = len(reader_of(TABLE_CALLS).scopes)
    assert calls + functions + 1 < scopes <= calls * functions + functions + 1


DEPTH = 1500


@pytest.mark.parametrize(
    ("body", "codes"),
    [
        pytest.param(
            "".join(f"def f{index}(h):\n    return f{index + 1}(h)\n" for index in range(DEPTH))
            + f"def f{DEPTH}(h):\n    return h()\ndef one():\n    return 3\ndef main():\n    return f0(one)\n",
            {1, 3},
            id="calls that pass a parameter on",
        ),
        pytest.param(
            "def main():\n    a0 = 3\n"
            + "".join(f"    a{index + 1} = a{index}\n" for index in range(DEPTH))
            + f"    return a{DEPTH}\n",
            {1, 3},
            id="locals bound each from the one before",
        ),
        pytest.param(
            "import sys\n"
            + "".join(f"def g{index}():\n    g{index + 1}()\n" for index in range(DEPTH))
            + f"def g{DEPTH}():\n    sys.exit(9)\ndef main():\n    g0()\n    return 0\n",
            {0, 1, 9},
            id="calls that reach an exit",
        ),
    ],
)
def test_the_reader_reads_calls_and_bindings_deeper_than_python_s_recursion_limit(body: str, codes: set[int]) -> None:
    # Each read waits its turn on the reader's own list, and each walk of calls or values keeps its own stack, so a
    # chain past Python's recursion limit, at which a reader that read a call inside the read that reached it raised
    # RecursionError, reads to its end.
    assert sys.getrecursionlimit() < DEPTH
    with mock.patch.object(exit_tables, "_bindings", wraps=exit_tables._bindings) as bindings:
        reader = reader_of(body)
    assert reader.reading.codes == codes
    # Each function's bindings are read from it once, so a chain of locals is read in time linear in its length.
    assert bindings.call_count <= len(reader.bindings)


def test_a_function_two_arguments_pass_on_from_one_scope_is_looked_up_there_once() -> None:
    # Each fi passes both its parameters on to f(i+1) inside two tables, so the function f20 calls is reached
    # through 2**20 paths of tables. callables reads each table once for the question, in each scope, so it looks up a
    # few names for each function; a set of reads begun anew in each scope it went out to took each path.
    chain = "".join(
        f"def f{index}(a, b):\n    return f{index + 1}({{'x': a, 'y': b}}['x'], {{'x': a, 'y': b}}['y'])\n"
        for index in range(20)
    )
    body = (
        f"def one():\n    return 3\ndef two():\n    return 4\n{chain}def f20(a, b):\n    return a()\n"
        "def main():\n    return f0(one, two)\n"
    )
    calls = 0
    original = exit_tables.CodeReader.function_named

    def function_named(self: exit_tables.CodeReader, *arguments: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1000:
            raise AssertionError("the reader looked up more than 1000 functions")
        return original(self, *arguments)

    with mock.patch.object(exit_tables.CodeReader, "function_named", function_named):
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
        # A call whose function one scope that called its own gives and another hides reads the function it is given
        # and is unread for the other.
        pytest.param(
            "def one():\n    return 3\ndef run(h):\n    return h()\ndef mid(g):\n    return run(g)\n"
            "def main(flag):\n    return mid(one) if flag else mid(lambda: 5)\n",
            {1, 3},
            ["h()"],
            id="a function one caller gives and another hides",
        ),
        # f is called back through w1 and through w2, each from a scope of h, which the call from f made.
        pytest.param(
            "def c():\n    return bool(argparse)\ndef one():\n    return 3\ndef two():\n    return 4\n"
            "def four():\n    return 6\n"
            "def f(q):\n    if c():\n        return q()\n"
            "    if c():\n        return h(two, w1)\n    return h(four, w2)\n"
            "def h(m, k):\n    t = m\n    return k(t)\ndef w1(x):\n    return f(x)\ndef w2(y):\n    return f(y)\n"
            "def main():\n    return f(one)\n",
            {1, 3, 4, 6},
            [],
            id="a function called back from two call sites",
        ),
        # A parameter read for two items of the tuple it is given, and a local bound in three scopes of one function,
        # each read in the same pass.
        pytest.param(
            "def pair():\n    return 3, 4\ndef first(t):\n    return t\n"
            "def main(flag):\n    a, b = first(pair())\n    return a if flag else b\n",
            {1, 3, 4},
            [],
            id="a parameter read for two items",
        ),
        pytest.param(
            "def leaf(p):\n    return p\ndef mid(x):\n    y = x\n    return leaf(y)\n"
            "def main(flag):\n    return mid(3) if flag else mid(4) if flag else mid(5)\n",
            {1, 3, 4, 5},
            [],
            id="a local bound in three scopes of one function",
        ),
        # leaf is read from mid(4)'s scope before mid(3)'s calls it, so a second pass reads it again, the exit call
        # that reaches it among the rest.
        pytest.param(
            "import sys\ndef leaf(p):\n    return p\ndef mid(x):\n    return leaf(x)\n"
            "def main(flag):\n    if flag:\n        sys.exit(mid(3) if flag else mid(4))\n    return 0\n",
            {0, 1, 3, 4},
            [],
            id="a function an exit call reaches from two scopes",
        ),
        # A parameter read both for an item, where it is returned, and whole, inside a tuple, is read each way.
        pytest.param(
            "def c():\n    return bool(argparse)\ndef pair():\n    return 3, 4\n"
            "def f(p):\n    if c():\n        return p\n    return (p, 1)\n"
            "def main():\n    code, _ = f(pair())\n    return code\n",
            {1, 3},
            ["(3, 4)"],
            id="a parameter read for an item and whole",
        ),
        # A call read for an item and one read whole each keep their callees' scopes apart, so g's h, given three
        # where k's result is unpacked, is never read as a code main returns.
        pytest.param(
            "def three():\n    return 5\ndef two():\n    return 4\ndef g(h):\n    return h()\n"
            "def k(h):\n    return g(h)\n"
            "def main(flag):\n    code, _ = k(three)\n    if flag:\n        return k(two)\n    return code\n",
            {1, 4},
            ["5"],
            id="calls read for an item and whole",
        ),
        # Within one scope, a call through a local one of whose values the reader cannot tell is unread, and reads
        # none of the others, as it can be any of them.
        pytest.param(
            "def one():\n    return 3\n"
            "def main(flag):\n    handler = one\n    handler = mystery\n    return handler()\n",
            {1},
            ["handler()"],
            id="a local one of whose values the reader cannot tell",
        ),
        # The scope that hides it is read second, so a later pass finds the call incomplete.
        pytest.param(
            "def one():\n    return 3\ndef run(h):\n    return h()\ndef mid(g):\n    return run(g)\n"
            "def main(flag):\n    return mid(lambda: 5) if flag else mid(one)\n",
            {1, 3},
            ["h()"],
            id="a function one caller gives and another, read later, hides",
        ),
        # An annotated local is read as a local bound by assignment is.
        pytest.param("def main():\n    code: int = 3\n    return code\n", {1, 3}, [], id="an annotated local"),
        # A local read both for an item, where it is returned, and whole, inside a tuple, is read each way.
        pytest.param(
            "def c():\n    return bool(argparse)\ndef pair():\n    return 3, 4\n"
            "def f():\n    p = pair()\n    if c():\n        return p\n    return (p, 1)\n"
            "def main():\n    code, _ = f()\n    return code\n",
            {1, 3},
            ["(3, 4)"],
            id="a local read for an item and whole",
        ),
        # A property is read in a scope of its own method, for the item asked of it.
        pytest.param(
            "class Tool:\n    @property\n    def pair(self):\n        return 3, 4\n"
            "def main(flag):\n    a, b = Tool().pair\n    return a if flag else b\n",
            {1, 3, 4},
            [],
            id="a property read for an item",
        ),
        pytest.param(
            "class Tool:\n    @property\n    def status(self):\n        code = 3\n        return code\n"
            "def main():\n    return Tool().status\n",
            {1, 3},
            [],
            id="a property that returns a local",
        ),
        # A raise of a name, met before an exit call in the same function, leaves the exit read.
        pytest.param(
            "import sys\ndef helper(flag):\n    if flag:\n        sys.exit(9)\n    raise ValueError\n"
            "def main():\n    helper(1)\n    return 0\n",
            {0, 1, 9},
            [],
            id="a raise before an exit",
        ),
        # A call of a name nothing binds is unread.
        pytest.param("def main():\n    return unknown()\n", {1}, ["unknown()"], id="a call of a name nothing binds"),
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
        # SystemExit takes no keyword and sys.exit its code by position alone, so Python refuses each call below with
        # a TypeError, which exits 1, while exit takes its code by keyword.
        ("def main() -> int:\n    helper()\n    return 0\ndef helper():\n    raise SystemExit(code=19)\n", {0, 1}),
        ("import sys\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    sys.exit(code=19)\n", {0, 1}),
        ("def main() -> int:\n    helper()\n    return 0\ndef helper():\n    exit(code=19)\n", {0, 1, 19}),
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


def test_a_table_of_functions_in_an_imported_module_is_read_in_that_module() -> None:
    # Each module's own body has a scope of its own, so the helper's table names its own function, not one of the
    # entry point's.
    body = "from helpers import run\ndef main() -> int:\n    return run('a')\n"
    body += "if __name__ == '__main__':\n    run_main(main)\n"
    helpers = "def four() -> int:\n    return 4\nTABLE = {'a': four}\ndef run(name) -> int:\n    return TABLE[name]()\n"
    found = problems(
        {
            "tools/tool.py": script("A tool.\n\nExit status:\n  1  failed\n  4  from the helper\n", body, HEADER),
            "tools/helpers.py": helpers,
        }
    )
    assert found == []


def test_a_run_main_target_that_is_a_call_is_read_as_a_call_not_as_the_function_it_names() -> None:
    # The lambda returns the function a table's get gives, never calls it, so run_main's code is that function, which
    # the reader cannot read as a code; the function's own code, 3, is never the exit.
    source = (
        "from console import run_main\ndef one():\n    return 3\nTABLE = {'a': one}\n"
        "if __name__ == '__main__':\n    run_main(lambda: TABLE.get('a'))\n"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "tool.py"
        path.write_text(source, encoding="utf-8")
        reader = exit_tables.read_codes(root, exit_tables.EntryPoint("tool.py", path, ast.parse(source)))
    assert reader.reading.codes == {1}
    assert [expression for _, _, expression in reader.reading.unread] == ["TABLE.get('a')"]


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


def test_unread_returns_are_reported_in_their_order_in_the_source() -> None:
    # The reader meets b's returns before a's, since it reads the last call it queues first, and which pass meets a
    # return depends on when its caller is found. The returns are reported by file, line, and column, an expression
    # met at two columns of one line by the first, so the report is the same however the passes run: on b's line,
    # other.second comes before another.first, which sorts before it by name.
    body = (
        "def c():\n    return bool(argparse)\n"
        "def a():\n    return thing.first\n"
        "def b():\n    return other.second if c() else (another.first if c() else other.second)\n"
        "def main():\n    return a() if c() else b()\n"
        f"{ENTRY}"
    )
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)})
    assert [problem.split(" an exit code")[0] for problem in found] == [
        "tools/tool.py:13 returns or exits with thing.first,",
        "tools/tool.py:15 returns or exits with other.second,",
        "tools/tool.py:15 returns or exits with another.first,",
    ]
    # The reader meets k.z, then h.y at its second column, and h.y again at its first, through g's parameter; h.y
    # is reported at its first column, so before k.z.
    body = (
        "def c():\n    return bool(argparse)\ndef g(x):\n    return x\n"
        "def main():\n    return g(h.y) if c() else (k.z if c() else h.y)\n"
        f"{ENTRY}"
    )
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)})
    assert [problem.split(" an exit code")[0] for problem in found] == [
        "tools/tool.py:15 returns or exits with h.y,",
        "tools/tool.py:15 returns or exits with k.z,",
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


def test_a_run_main_target_the_reader_cannot_tell_is_read_as_a_returned_expression() -> None:
    # A table one of whose values the reader cannot tell names no function it can follow, so the target is unread.
    body = (
        "def one():\n    return 3\nHANDLERS = {'a': one, 'b': thing.attr}\n"
        "if __name__ == '__main__':\n    run_main(HANDLERS['a'])\n"
    )
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", body)})
    assert [problem.split(", an exit code")[0] for problem in found] == [
        "tools/tool.py:14 returns or exits with HANDLERS['a']"
    ]


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


def test_a_relative_import_past_its_package_s_top_names_no_module() -> None:
    # The policy kept its own reader of relative imports, which read `from ..tools import run` in the deployer
    # package as the repository's tools.run, though Python refuses an import past the package's top; it reads them
    # through validation_support's one reader now.
    tree = ast.parse("from ..tools import run\nfrom .errors import DeployError\n")
    module = exit_tables.Module.read(Path("deployer/cli.py"), tree, "deployer")
    assert module.aliases == {"DeployError": "deployer.errors.DeployError"}

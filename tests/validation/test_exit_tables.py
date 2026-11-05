"""Fixture tests for tests/validation/exit_tables.py.

Each check fails on a violating fixture tree and passes on a conforming one. The fixtures are text in string literals.
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import exit_tables
from validation_support import write_fixture_tree

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
        ("import sys\ndef main() -> int:\n    helper()\n    return 0\ndef helper():\n    sys.exit(9)\n", {0, 1, 9}),
        ("def main() -> int:\n    helper()\n    return 0\ndef helper():\n    raise SystemExit(15)\n", {0, 1, 15}),
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
    assert unread[0].startswith("tools/tool.py:11 returns thing.code, an exit code the exit-table reader cannot follow")
    allowed = "EXIT_CODES_ALLOWED = {'thing.code': 'the thing says 0 or 2'}\n"
    assert problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", allowed + body)}) == []
    stale = "EXIT_CODES_ALLOWED = {'other.code': 'nothing returns it'}\n"
    found = problems({"tools/tool.py": script(f"A tool.\n\n{TABLE}", stale + MAIN)})
    assert found == [
        "tools/tool.py names 'other.code' in EXIT_CODES_ALLOWED, which is no return the exit-table reader cannot "
        "follow from an entry point; remove it"
    ]


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

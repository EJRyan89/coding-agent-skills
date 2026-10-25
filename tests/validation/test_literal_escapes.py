"""Fixture tests for tests/validation/literal_escapes.py.

The policy fails a non-raw literal whose control escape reads as a path or a pattern and passes one that means the
control character.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from literal_escapes import escape_files, literal_escape_problems

# Pinned here as a literal, not read back from the policy.
REFUSAL = (
    "writes {escapes} in a non-raw literal that reads as a path or a pattern; make it raw, or escape the backslash, "
    "or state why in ESCAPE_ALLOWED"
)


def problems(root: Path, source: str, name: str = "tests/test_sample.py") -> list[str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return literal_escape_problems(root, [path])


@pytest.mark.parametrize(
    ("source", "escapes"),
    [
        pytest.param('ROOT = "\\bin"\n', "\\b", id="a rooted path as a backspace"),
        pytest.param('TEMP = "C:\\temp"\n', "\\t", id="a drive path as a tab"),
        pytest.param('import re\nWORD = re.compile("\\bword")\n', "\\b", id="a pattern opening with a backspace"),
        pytest.param('import re\nWORD = re.compile("(?i)x\\bword")\n', "\\b", id="a pattern's word boundary"),
        pytest.param('from re import search as s\nX = s("a \\b", "")\n', "\\b", id="re through an alias"),
        pytest.param('import pathlib\nP = pathlib.Path("a\\nb")\n', "\\n", id="a path a constructor takes"),
        pytest.param('from pathlib import PureWindowsPath\nP = PureWindowsPath("a", "\\t")\n', "\\t", id="a part"),
        pytest.param('ROOT = "C:\\\\Users\\bin"\n', "\\b", id="after an escaped backslash"),
        pytest.param('NEW = "dir/\\new"\n', "\\n", id="after a slash"),
        pytest.param('BIN = f"{ROOT}\\bin"\n', "\\b", id="after an f-string field"),
        pytest.param('BIN = b"\\bin"\n', "\\b", id="bytes"),
        pytest.param('BIN = ("ok\\n"\n    "\\bin")\n', "\\b", id="a later piece of a concatenation"),
        pytest.param('TEMP = "D:\\tmp and C:\\new"\n', "\\n, \\t", id="two escapes in one literal"),
        pytest.param('def f() -> str:\n    """Reads C:\\temp."""\n    return ""\n', "\\t", id="a docstring"),
    ],
)
def test_a_literal_that_reads_as_a_path_or_pattern_fails(tmp_path: Path, source: str, escapes: str) -> None:
    line = source.count("\n", 0, source.index('"')) + 1
    assert problems(tmp_path, source) == [f"tests/test_sample.py:{line} {REFUSAL.format(escapes=escapes)}"]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('ROOT = r"\\bin"\n', id="a raw rooted path"),
        pytest.param('TEMP = r"C:\\temp"\n', id="a raw drive path"),
        pytest.param('TEMP = "C:\\\\temp"\n', id="an escaped backslash"),
        pytest.param('ROOT = "C:\\\\Users\\\\bin"\n', id="escaped backslashes throughout"),
        pytest.param('import re\nWORD = re.compile(r"\\bword")\n', id="a raw pattern"),
        pytest.param('BIN = rb"\\bin" + Rb"\\bin" + bR"\\bin"\n', id="raw bytes"),
        pytest.param('BIN = rf"{ROOT}\\bin"\n', id="a raw f-string"),
        pytest.param('NEWLINE = "\\n"\n', id="a newline alone"),
        pytest.param('LINE = "line\\n"\n', id="a line ending in a newline"),
        pytest.param('LINES = "\\nNext line\\tafter a tab"\n', id="text opening with a newline"),
        pytest.param('KEY = "name:\\n" + "a:\\n\\tb"\n', id="a YAML key closing its line"),
        pytest.param('BREAK = "a\\\\\\nb"\n', id="a Markdown line break"),
        pytest.param('PAGE = "\\fKeep one queue."\n', id="a form feed opening a page"),
        pytest.param('FIELD = f"{name}\\n{value}\\tdone"\n', id="text after an f-string field"),
        pytest.param('import re\nLINES = re.compile("a\\nb\\t")\n', id="a pattern holding a newline and a tab"),
        pytest.param('import pathlib\nP = pathlib.Path("C:/temp")\n', id="a path with no escape"),
        pytest.param('def f() -> None:\n    """Joins C:\\\\temp."""\n', id="an escaped docstring"),
        pytest.param('BELL = "\\a"\n', id="a bell alone"),
        pytest.param('SPACE = " \\t\\f\\v"\n', id="whitespace escapes side by side"),
    ],
)
def test_a_literal_that_means_the_control_character_passes(tmp_path: Path, source: str) -> None:
    assert problems(tmp_path, source) == []


def test_an_allowance_sanctions_its_scope_and_an_unused_entry_fails(tmp_path: Path) -> None:
    sanctioned = (
        'ESCAPE_ALLOWED = {"f": "it writes the backspace the terminal reads"}\ndef f() -> str:\n    return "\\bin"\n'
    )
    assert problems(tmp_path, sanctioned) == []
    stale = 'ESCAPE_ALLOWED = {"g": "gone"}\nOK = "line\\n"\n'
    assert problems(tmp_path, stale) == [
        "tests/test_sample.py: ESCAPE_ALLOWED allows g, which holds nothing it sanctions; remove the entry"
    ]
    other = 'ESCAPE_ALLOWED = {"f": "another scope"}\ndef f() -> None: ...\nBIN = "\\bin"\n'
    assert problems(tmp_path, other) == [
        "tests/test_sample.py:3 " + REFUSAL.format(escapes="\\b"),
        "tests/test_sample.py: ESCAPE_ALLOWED allows f, which holds nothing it sanctions; remove the entry",
    ]


def test_the_policy_reads_every_formatted_file_suites_included(tmp_path: Path) -> None:
    names = [
        "tests/test_sample.py",
        "skills/x/scripts/test_x.py",
        "skills/x/scripts/x.py",
        "deployer/a.py",
        "tools/b.py",
        ".claude/skills/y/scripts/y.py",
        "deploy.py",
        "conftest.py",
    ]
    outside = ["docs/example.py", "tests/notes.txt"]
    files = []
    for name in [*names, *outside]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('BIN = "\\bin"\n', encoding="utf-8")
        files.append(path)
    assert sorted(path.relative_to(tmp_path).as_posix() for path in escape_files(tmp_path, files)) == sorted(names)
    assert len(literal_escape_problems(tmp_path, files)) == len(names)

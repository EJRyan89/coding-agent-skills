"""Fixture tests for tests/validation/line_numbering.py.

The policy fails a count of line breaks from a text's start that runs once per position, in a loop or in a function a
loop of the module calls, and passes a count that runs once, one from another start, and a numbering over the text's
line starts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from line_numbering import line_numbering_problems
from validation_support import write_fixture_tree

# Pinned here as a literal, not read back from the policy.
REFUSAL = (
    "counts line breaks from the text's start once per position, in a loop or a function a loop calls, in time that "
    "grows with the square of the text; read the text's text_lines.line_starts once and take each position's line "
    "with text_lines.line_index, or state why in LINE_COUNT_ALLOWED"
)
MODULE = "skills/alpha/scripts/alpha.py"


def problems(root: Path, source: str, name: str = MODULE) -> list[str]:
    write_fixture_tree(root, {name: source})
    return line_numbering_problems(root, sorted(root.rglob("*.py")))


# Each source marks the line of each count the policy refuses with `# read`.
REFUSED = [
    pytest.param(
        "def f(text, pattern):\n    for match in pattern.finditer(text):\n"
        "        yield text.count('\\n', 0, match.start())  # read\n",
        id="a count from 0 in a for loop over finditer",
    ),
    pytest.param(
        "def f(text, pattern):\n    return [text.count('\\n', 0, m.start()) for m in pattern.finditer(text)]  # read\n",
        id="a count from 0 in a comprehension",
    ),
    pytest.param(
        "def f(text, places):\n    return {text.count(chr(10), 0, place) + 1 for place in places}  # read\n",
        id="chr(10) in a set comprehension over anything",
    ),
    pytest.param(
        "def f(text, places):\n    return {place: text.count('\\r\\n', 0, place) for place in places}  # read\n",
        id="a CRLF in a dict comprehension",
    ),
    pytest.param(
        "def f(data, places):\n    return [data.count(b'\\n', 0, place) for place in places]  # read\n",
        id="a bytes line feed",
    ),
    pytest.param(
        "def f(text, places):\n    return [str.count(text, '\\r', 0, place) for place in places]  # read\n",
        id="an unbound count of a CR",
    ),
    pytest.param(
        "def f(text, places):\n    return [text[:place].count('\\n') for place in places]  # read\n",
        id="a count of a slice from the start",
    ),
    pytest.param(
        "def f(text, places):\n    return [text[0:place].count('\\n') for place in places]  # read\n",
        id="a count of a slice from 0",
    ),
    pytest.param(
        "def f(text, places):\n    found = []\n    while places:\n"
        "        found.append(text.count('\\n', 0, places.pop()))  # read\n    return found\n",
        id="a count in a while loop's body",
    ),
    pytest.param(
        "def f(text, place):\n    while text.count('\\n', 0, place) > 3:  # read\n"
        "        place -= 1\n    return place\n",
        id="a count in a while loop's test",
    ),
    pytest.param(
        "async def f(text, places):\n    async for place in places:\n"
        "        yield text.count('\\n', 0, place)  # read\n",
        id="a count in an async for loop",
    ),
    pytest.param(
        "def f(texts, place):\n    return [t for text in texts for t in [text.count('\\n', 0, place)]]  # read\n",
        id="a count in a comprehension's second iterable",
    ),
    pytest.param(
        "def f(text, places):\n    return [p for p in places if text.count('\\n', 0, p) > 1]  # read\n",
        id="a count in a comprehension's condition",
    ),
    pytest.param(
        "from text_lines import text_lines\n\n\ndef f(text, places):\n"
        "    return [len(text_lines(text[:place], keepends=True)) for place in places]  # read\n",
        id="text_lines of a slice from the start",
    ),
    pytest.param(
        "import text_lines\n\n\ndef f(text, places):\n"
        "    return [len(text_lines.lines_and_breaks(text[:place])[0]) for place in places]  # read\n",
        id="lines_and_breaks of a slice through the module",
    ),
    pytest.param(
        "from text_lines import line_starts as starts\n\n\ndef f(text, places):\n"
        "    return [len(starts(text[:place])) for place in places]  # read\n",
        id="line_starts of a slice through an alias",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef f(text, places):\n"
        "    return [len(LINE_BREAK.findall(text, 0, place)) for place in places]  # read\n",
        id="LINE_BREAK findall from 0",
    ),
    pytest.param(
        "from text_lines import LINE_FEED\n\n\ndef f(text, places):\n"
        "    return [sum(1 for _ in LINE_FEED.finditer(text, 0, place)) for place in places]  # read\n",
        id="LINE_FEED finditer from 0",
    ),
    pytest.param(
        "import text_lines\n\n\ndef f(text, places):\n"
        "    return [len(text_lines.LINE_BREAK.split(text[:place])) for place in places]  # read\n",
        id="LINE_BREAK split of a slice from the start",
    ),
    pytest.param(
        "def line_of(text, index):\n    return text.count('\\n', 0, index) + 1  # read\n\n\n"
        "def f(text, places):\n    return [line_of(text, place) for place in places]\n",
        id="a function a comprehension calls by its name",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef line_of(text, index):\n"
        "    return len(LINE_BREAK.findall(text, 0, index)) + 1  # read\n\n\n"
        "def f(text, spans):\n    for first, _ in spans:\n        yield line_of(text, first)\n",
        id="a function a for loop calls by its name, as #3013's body check did",
    ),
    pytest.param(
        "class Reader:\n    def line_of(self, index):\n        return self.text.count('\\n', 0, index)  # read\n\n"
        "    def lines(self, places):\n        return [self.line_of(place) for place in places]\n",
        id="a method a loop calls on self",
    ),
    pytest.param(
        "LINE_COUNT_ALLOWED = {'g': 'a reason'}\n\n\ndef f(text, places):\n"
        "    return [text.count('\\n', 0, p) for p in places]  # read\n\n\n"
        "def g(text, places):\n    return [text.count('\\n', 0, p) for p in places]\n",
        id="a count outside the scope the allowance names",
    ),
]

PASSED = [
    pytest.param("def f(text, place):\n    return text.count('\\n', 0, place) + 1\n", id="a count run once"),
    pytest.param(
        "def f(text, pattern):\n    match = pattern.search(text)\n"
        "    lines = text.count('\\n', 0, match.start())\n    for _ in range(lines):\n        yield lines\n",
        id="a count before a loop",
    ),
    pytest.param(
        "def f(text, places):\n    position, line, found = 0, 1, []\n    for place in places:\n"
        "        line += text.count('\\n', position, place)\n        position = place\n"
        "        found.append(line)\n    return found\n",
        id="a count from the previous position",
    ),
    pytest.param(
        "def f(text, places):\n    return [text.count(',', 0, place) for place in places]\n",
        id="a count of another character",
    ),
    pytest.param("def f(texts):\n    return [text.count('\\n') for text in texts]\n", id="a count of each whole text"),
    pytest.param(
        "def f(text, places):\n    return [text[place:].count('\\n') for place in places]\n",
        id="a count of a slice to the end",
    ),
    pytest.param(
        "def f(text, spans):\n    return [text[a:b].count('\\n') for a, b in spans]\n", id="a count of each span"
    ),
    pytest.param(
        "def f(texts):\n    return [text.count('\\n', 0) for text in texts]\n",
        id="a count from 0 of each whole text, given no end",
    ),
    pytest.param(
        "from text_lines import line_index, line_starts\n\n\ndef f(text, pattern):\n    starts = line_starts(text)\n"
        "    return [line_index(starts, match.start()) for match in pattern.finditer(text)]\n",
        id="each position numbered over the line starts",
    ),
    pytest.param(
        "from text_lines import line_starts\n\n\ndef f(texts):\n    return [line_starts(text) for text in texts]\n",
        id="the line starts of each whole text",
    ),
    pytest.param(
        "import re\n\nOTHER = re.compile(',')\n\n\ndef f(text, places):\n"
        "    return [len(OTHER.findall(text, 0, place)) for place in places]\n",
        id="another pattern's findall from 0",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef f(texts):\n"
        "    return [LINE_BREAK.split(text, 0) for text in texts]\n",
        id="a split given maxsplit 0 of each whole text",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef f(texts):\n"
        "    return [LINE_BREAK.findall(text, 0) for text in texts]\n",
        id="a findall from 0 of each whole text, given no end",
    ),
    pytest.param(
        "def line_of(text, index):\n    return text.count('\\n', 0, index) + 1\n\n\n"
        "def f(text, place):\n    return line_of(text, place)\n",
        id="a function called once",
    ),
    pytest.param(
        "LINE_COUNT_ALLOWED = {'f': 'numbers a handful of places a caller bounds'}\n\n\n"
        "def f(text, places):\n    return [text.count('\\n', 0, p) for p in places]\n",
        id="a count its allowance sanctions",
    ),
]


@pytest.mark.parametrize("source", REFUSED)
def test_a_count_from_a_text_s_start_once_per_position_fails(tmp_path: Path, source: str) -> None:
    lines = [number for number, text in enumerate(source.split("\n"), 1) if text.endswith("# read")]
    assert problems(tmp_path, source) == [f"{MODULE}:{line} {REFUSAL}" for line in lines]


@pytest.mark.parametrize("source", PASSED)
def test_a_count_that_runs_once_or_from_elsewhere_passes(tmp_path: Path, source: str) -> None:
    assert problems(tmp_path, source) == []


def test_an_allowance_entry_that_sanctions_nothing_fails(tmp_path: Path) -> None:
    source = "LINE_COUNT_ALLOWED = {'f': 'a reason'}\n\n\ndef f(text, place):\n    return text.count('\\n', 0, place)\n"
    assert problems(tmp_path, source) == [
        f"{MODULE}: LINE_COUNT_ALLOWED allows f, which holds nothing it sanctions; remove the entry"
    ]


def test_a_test_script_is_not_read(tmp_path: Path) -> None:
    source = "def test_f(text, places):\n    assert [text.count('\\n', 0, p) for p in places]\n"
    write_fixture_tree(tmp_path, {MODULE: "", "skills/alpha/scripts/test_alpha.py": source})
    assert line_numbering_problems(tmp_path, sorted(tmp_path.rglob("*.py"))) == []


def test_a_policy_that_read_no_module_says_so(tmp_path: Path) -> None:
    assert line_numbering_problems(tmp_path, []) == [
        "no Python module was read, so the line-numbering policy could not judge anything"
    ]

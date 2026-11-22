"""Fixture tests for tests/validation/line_numbering.py.

The policy fails a count of line breaks from a text's start that runs once per position, in a loop or in a function a
loop of the module calls, and a text's line starts built from a line-break pattern outside text_lines.py, and passes a
count that runs once, one from another start, a numbering over the text's line starts, and each form the policy's
stated limits leave unread.
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
STARTS_REFUSAL = (
    "builds a text's line starts from a line-break pattern itself; take them from text_lines.line_starts, and each "
    "position's line with text_lines.line_index, or state why in LINE_COUNT_ALLOWED"
)
# The refusal each marker at a line's end names.
MARKED = {"# read": REFUSAL, "# starts": STARTS_REFUSAL}
MODULE = "skills/alpha/scripts/alpha.py"


def problems(root: Path, source: str, name: str = MODULE) -> list[str]:
    write_fixture_tree(root, {name: source})
    return line_numbering_problems(root, sorted(root.rglob("*.py")))


# Each source marks the line of each count the policy refuses with `# read`, and of each line starts built from a
# line-break pattern with `# starts`.
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
    pytest.param(
        "def f(text, places):\n    return [text.count(chr(13), 0, p) for p in places]  # read\n",
        id="chr(13) from 0",
    ),
    pytest.param(
        "def f(data, places):\n    return [data.count(b'\\r', 0, place) for place in places]  # read\n",
        id="a bytes CR",
    ),
    pytest.param(
        "def f(data, places):\n    return [data[:place].count(b'\\r\\n') for place in places]  # read\n",
        id="a bytes CRLF of a slice from the start",
    ),
    pytest.param(
        "def f(text, ps):\n    return [len(text[:p].splitlines()) for p in ps]  # read\n",
        id="splitlines of a slice from the start",
    ),
    pytest.param(
        "def f(text, ps):\n    return [len(str.splitlines(text[:p])) for p in ps]  # read\n",
        id="an unbound splitlines of a slice from the start",
    ),
    pytest.param(
        "def f(text, ps):\n    return [len(text[:p].split('\\n')) for p in ps]  # read\n",
        id="a split at LF of a slice from the start",
    ),
    pytest.param(
        "def f(data, ps):\n    return [len(data[:p].split(b'\\n')) for p in ps]  # read\n",
        id="a bytes split at LF of a slice from the start",
    ),
    pytest.param(
        "def f(text, ps):\n    return [len(text[0:p].rsplit(sep=chr(10))) for p in ps]  # read\n",
        id="an rsplit by sep of a slice from 0",
    ),
    pytest.param(
        "def f(text, ps):\n    return [len(str.split(text[:p], '\\r\\n')) for p in ps]  # read\n",
        id="an unbound split at CRLF of a slice from the start",
    ),
    pytest.param(
        "from text_lines import program_lines\n\n\ndef f(output, places):\n"
        "    return [len(program_lines(output[:place])) for place in places]  # read\n",
        id="program_lines of a slice from the start",
    ),
    pytest.param(
        "from text_lines import KEPT_LINE\n\n\ndef f(text, places):\n"
        "    return [len(KEPT_LINE.findall(text, 0, place)) for place in places]  # read\n",
        id="KEPT_LINE findall from 0",
    ),
    pytest.param(
        "class Reader:\n    @classmethod\n    def line_of(cls, text, index):\n"
        "        return text.count('\\n', 0, index)  # read\n\n"
        "    @classmethod\n    def lines(cls, text, places):\n        return [cls.line_of(text, p) for p in places]\n",
        id="a method a loop calls on cls",
    ),
    pytest.param(
        "import re\n\nLINE = re.compile('\\n')\n\n\ndef f(text, places):\n"
        "    return [len(LINE.findall(text, 0, place)) for place in places]  # read\n",
        id="a module's own compiled line feed from 0",
    ),
    pytest.param(
        "import re\n\nBREAK: re.Pattern[str] = re.compile(r'\\r\\n|\\r|\\n')\n\n\ndef f(text, places):\n"
        "    return [len(BREAK.split(text[:place])) for place in places]  # read\n",
        id="a module's own annotated compiled break split on a slice",
    ),
    pytest.param(
        "import re\n\n\ndef f(text, places):\n"
        "    return [len(re.compile(b'\\n').findall(text[:p])) for p in places]  # read\n",
        id="a bytes compile in place on a slice",
    ),
    pytest.param(
        "import re\n\n\ndef f(text, places):\n    return [len(re.findall('\\n', text[:p])) for p in places]  # read\n",
        id="re.findall of a line feed on a slice",
    ),
    pytest.param(
        "from re import split\n\n\ndef f(text, places):\n"
        "    return [len(split(r'\\n|\\r', text[:p])) for p in places]  # read\n",
        id="re.split through an import on a slice",
    ),
    # Stated: each text is read once, but the policy cannot tell many texts from one.
    pytest.param(
        "def f(texts):\n    return [text[:-1].count('\\n') for text in texts]  # read\n",
        id="the start of each of many texts, which the policy refuses as stated",
    ),
    pytest.param(
        "def line_of(text, index):\n    return text.count('\\n', 0, index)  # read\n\n\n"
        "class Reader:\n    def line_of(self, index):\n        return index\n\n"
        "    def lines(self, places):\n        return [self.line_of(p) for p in places]\n",
        id="a function sharing its name with a method a loop calls, which the policy refuses as stated",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef f(text):\n"
        "    return [0, *(match.end() for match in LINE_BREAK.finditer(text))]  # starts\n",
        id="line starts built from LINE_BREAK's matches",
    ),
    pytest.param(
        "import re\n\n\ndef f(text):\n    return [0, *(m.end() for m in re.finditer('\\n', text))]  # starts\n",
        id="line starts built from re.finditer of a line feed",
    ),
    pytest.param(
        "import re\n\nFEED = re.compile(r'\\n')\n\n\ndef f(text):\n"
        "    return {m.end(): m.start() for m in FEED.finditer(text)}  # starts\n",
        id="line starts keyed from a module's own compiled line feed",
    ),
    pytest.param(
        "import text_lines\n\n\ndef f(text):\n    starts = [0]\n"
        "    for match in text_lines.LINE_FEED.finditer(text):  # starts\n        starts.append(match.end())\n"
        "    return starts\n",
        id="line starts appended in a for loop",
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
    pytest.param(
        "def f(text, place):\n    return [n for n in range(text.count('\\n', 0, place))]\n",
        id="a count in a comprehension's first iterable, which runs once",
    ),
    pytest.param(
        "def f(text, places, place):\n    for p in places:\n        print(p)\n    else:\n"
        "        return text.count('\\n', 0, place)\n",
        id="a count in a for loop's else, which runs once",
    ),
    pytest.param(
        "def f(text, place):\n    while place:\n        place -= 1\n    else:\n"
        "        return text.count('\\n', 0, place)\n",
        id="a count in a while loop's else, which runs once",
    ),
    pytest.param(
        "def f(texts):\n    return [len(text.split('\\n')) for text in texts]\n", id="a split of each whole text"
    ),
    pytest.param(
        "def f(text, places):\n    return [len(text[:p].split(',')) for p in places]\n",
        id="a split of a slice at another separator",
    ),
    pytest.param(
        "def f(text, places):\n    return [len(text[:p].split()) for p in places]\n",
        id="a split of a slice at whitespace",
    ),
    pytest.param(
        "import re\n\n\ndef f(texts):\n    return [len(re.findall('\\n', text)) for text in texts]\n",
        id="re.findall of each whole text, a stated limit",
    ),
    pytest.param(
        "import re\n\nCLAUSE = re.compile(r'[.;:](?=\\s|$)')\n\n\ndef f(text):\n"
        "    return [0, *(m.end() for m in CLAUSE.finditer(text))]\n",
        id="ends of another pattern's matches",
    ),
    pytest.param(
        "from text_lines import LINE_BREAK\n\n\ndef f(text):\n"
        "    return [m.start() for m in LINE_BREAK.finditer(text)]\n",
        id="where each line break starts",
    ),
    pytest.param(
        "NEWLINE = '\\n'\n\n\ndef f(text, places):\n    return [text.count(NEWLINE, 0, p) for p in places]\n",
        id="a line break through a name, a stated limit",
    ),
    pytest.param(
        "import re\n\n\ndef f(text, places):\n    line = re.compile('\\n')\n"
        "    return [len(line.findall(text, 0, p)) for p in places]\n",
        id="a pattern a function binds, a stated limit",
    ),
    pytest.param(
        "def f(text, places):\n    return list(map(lambda p: text.count('\\n', 0, p), places))\n",
        id="a count in a lambda map runs, a stated limit",
    ),
    pytest.param(
        "def f(text, places):\n    return sorted(places, key=lambda p: text.count('\\n', 0, p))\n",
        id="a count in a sort key, a stated limit",
    ),
    pytest.param(
        "from functools import partial\n\n\ndef line_of(text, index):\n    return text.count('\\n', 0, index)\n\n\n"
        "def f(text, places):\n    return list(map(partial(line_of, text), places))\n",
        id="a function map runs through partial, a stated limit",
    ),
    pytest.param(
        "class Reading:\n    def line_of(self, index):\n        return self.text.count('\\n', 0, index)\n\n\n"
        "def f(reading, places):\n    return [reading.line_of(p) for p in places]\n",
        id="a method a loop calls on another receiver, a stated limit",
    ),
]


@pytest.mark.parametrize("source", REFUSED)
def test_a_count_from_a_text_s_start_once_per_position_fails(tmp_path: Path, source: str) -> None:
    expected = [
        f"{MODULE}:{number} {refusal}"
        for number, text in enumerate(source.split("\n"), 1)
        for marker, refusal in MARKED.items()
        if text.endswith(marker)
    ]
    assert problems(tmp_path, source) == expected


@pytest.mark.parametrize("source", PASSED)
def test_a_count_that_runs_once_or_from_elsewhere_passes(tmp_path: Path, source: str) -> None:
    assert problems(tmp_path, source) == []


def test_a_function_another_module_calls_in_a_loop_passes_as_a_stated_limit(tmp_path: Path) -> None:
    helper = "def line_of(text, index):\n    return text.count('\\n', 0, index)\n"
    caller = "from helper import line_of\n\n\ndef f(text, places):\n    return [line_of(text, p) for p in places]\n"
    write_fixture_tree(tmp_path, {"skills/alpha/scripts/helper.py": helper, MODULE: caller})
    assert line_numbering_problems(tmp_path, sorted(tmp_path.rglob("*.py"))) == []


def test_text_lines_builds_the_line_starts_it_gives(tmp_path: Path) -> None:
    source = (
        "from text_lines import LINE_BREAK\n\n\ndef line_starts(text):\n"
        "    return [0, *(match.end() for match in LINE_BREAK.finditer(text))]\n"
    )
    assert problems(tmp_path, source, "skills/ejryan89-skill-core/scripts/text_lines.py") == []


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

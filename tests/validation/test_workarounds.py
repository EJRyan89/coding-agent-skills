"""Fixture tests for tests/validation/workarounds.py.

Each policy fails on a violating input and passes on a conforming one. The fixtures are text in string literals, so
the policies, which read comments and names, find nothing in this file itself.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from validation_support import write_fixture_tree
from workarounds import catalogue_test_problems, later_comment_problems, not_read_problems, skip_problems

SUITE = "tests/test_fixture.py"


def fixture_problems(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """The skip and the comment problems of a fixture tree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, files)
        paths = [root / name for name in files]
        return skip_problems(root, paths), later_comment_problems(root, paths)


def fixture_skips(source: str) -> list[str]:
    return fixture_problems({SUITE: source})[0]


@pytest.mark.parametrize(
    ("source", "marker"),
    [
        pytest.param(
            "import unittest\nclass C:\n    @unittest.skip('later')\n    def test_a(self): ...\n",
            "unittest.skip",
            id="unittest.skip",
        ),
        pytest.param(
            "import unittest as u\nclass C(u.TestCase):\n    @u.expectedFailure\n    def test_a(self): ...\n",
            "unittest.expectedFailure",
            id="unittest.expectedFailure",
        ),
        pytest.param(
            "from unittest import skipIf\nclass C:\n    @skipIf(True, 'slow')\n    def test_a(self): ...\n",
            "unittest.skipIf",
            id="unittest.skipIf",
        ),
        pytest.param(
            "import unittest\nclass C:\n    @unittest.skipUnless(FAST, 'slow')\n    def test_a(self): ...\n",
            "unittest.skipUnless",
            id="unittest.skipUnless",
        ),
        pytest.param("class C:\n    def test_a(self):\n        self.skipTest('flaky')\n", "skipTest", id="skipTest"),
        pytest.param(
            "import unittest\nclass C:\n    def test_a(self):\n        raise unittest.SkipTest('flaky')\n",
            "unittest.SkipTest",
            id="unittest.SkipTest",
        ),
        pytest.param(
            "from unittest.case import skip\nclass C:\n    @skip('later')\n    def test_a(self): ...\n",
            "unittest.case.skip",
            id="unittest.case.skip",
        ),
        pytest.param(
            "import pytest\n@pytest.mark.xfail\ndef test_a(): ...\n", "pytest.mark.xfail", id="pytest.mark.xfail"
        ),
    ],
)
def test_each_skip_and_expected_failure_fails_through_the_module_imports(source: str, marker: str) -> None:
    problems = fixture_skips(source)
    assert len(problems) == 1, problems
    assert f"with {marker}, so its suite passes without it" in problems[0]
    assert problems[0].startswith(f"{SUITE}:"), problems[0]


@pytest.mark.parametrize(
    "condition",
    [
        "sys.platform == 'win32'",
        "sys.platform != 'win32'",
        "os.name == 'nt' and sys.platform in ('linux', 'darwin')",
        "not sys.platform.startswith('linux')",
        "platform.system() == 'Windows'",
    ],
)
def test_a_skip_on_a_platform_alone_passes(condition: str) -> None:
    source = (
        "import os, platform, sys, unittest\nclass C(unittest.TestCase):\n"
        f"    @unittest.skipIf({condition}, 'not on this platform')\n    def test_a(self): ...\n"
    )
    assert fixture_skips(source) == []
    pytest_source = (
        "import os, platform, sys\nimport pytest\n"
        f"@pytest.mark.skipif({condition}, reason='not on this platform')\ndef test_a(): ...\n"
    )
    assert fixture_skips(pytest_source) == []


@pytest.mark.parametrize(
    "condition", ["sys.platform == 'win32' or SLOW", "sys.platform == PLATFORM", "os.environ.get('CI')"]
)
def test_a_skip_on_any_other_condition_fails(condition: str) -> None:
    source = (
        "import os, sys, unittest\nclass C(unittest.TestCase):\n"
        f"    @unittest.skipUnless({condition}, 'not here')\n    def test_a(self): ...\n"
    )
    assert len(fixture_skips(source)) == 1
    pytest_source = (
        f"import os, sys\nfrom pytest import mark\n@mark.skipif({condition}, reason='not here')\ndef test_a(): ...\n"
    )
    assert len(fixture_skips(pytest_source)) == 1


class WorkaroundFixtures(unittest.TestCase):
    def test_a_test_a_class_body_defines_on_one_platform_passes(self) -> None:
        source = (
            "import sys, unittest\nclass C(unittest.TestCase):\n"
            "    if sys.platform == 'win32':\n        def test_a(self): ...\n"
        )
        self.assertEqual(([], []), fixture_problems({SUITE: source}))

    def test_an_allowance_sanctions_its_skip_and_fails_when_it_sanctions_nothing(self) -> None:
        skipped = "import unittest\nclass C(unittest.TestCase):\n    @unittest.skip('x')\n    def test_a(self): ...\n"
        allowed = "SKIP_ALLOWED = {'C.test_a': 'the service it calls is gone'}\n"
        self.assertEqual([], fixture_skips(allowed + skipped))
        stale = fixture_skips("SKIP_ALLOWED = {'C.test_b': 'no longer skipped'}\n")
        self.assertEqual(
            [f"{SUITE} SKIP_ALLOWED names C.test_b, which holds no skip or expected failure; remove the entry"], stale
        )
        unreadable = fixture_skips("SKIP_ALLOWED = {'C.test_a': ''}\n" + skipped)
        self.assertIn(f"{SUITE} SKIP_ALLOWED must map each function or class to its reason", unreadable)
        self.assertEqual(2, len(unreadable))

    def test_a_module_level_skip_is_named_by_the_module(self) -> None:
        problems = fixture_skips("import pytest\npytest.importorskip('numpy')\n")
        self.assertEqual(1, len(problems))
        self.assertIn("marks <module> with pytest.importorskip", problems[0])

    def test_a_comment_that_leaves_work_for_later_fails_and_text_in_a_string_passes(self) -> None:
        tag = "TO" + "DO"
        source = (
            f"x = 1  # {tag}: finish\n"
            "# FIXME the parser\n"
            "# XXX\n"
            f"TEXT = '# {tag} in a string is no comment'\n"
            "# a template such as tmpXXXXXX, a todo in lower case, and FIXMES are not markers\n"
        )
        comments = fixture_problems({SUITE: source})[1]
        self.assertEqual(
            [f"{SUITE}:1 leaves {tag}", f"{SUITE}:2 leaves FIXME", f"{SUITE}:3 leaves XXX"],
            [problem.split(" in a comment")[0] for problem in comments],
        )

    def test_files_outside_the_checked_python_pass(self) -> None:
        later = "# FIXME the parser\n"
        skipped = "import unittest\n@unittest.skip('x')\nclass C: ...\n"
        self.assertEqual(
            ([], []),
            fixture_problems(
                {
                    "tests/fixtures/repo/test_x.py": later + skipped,
                    "docs/notes.py": later + skipped,
                    "skills/a/scripts/run.sh": later,
                }
            ),
        )
        self.assertEqual(1, len(fixture_problems({"skills/a/scripts/test_x.py": later})[1]))
        self.assertEqual(1, len(fixture_problems({"deploy.py": later})[1]))


def fixture_not_read(files: dict[str, str]) -> list[str]:
    """The NOT_READ problems of a fixture tree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, files)
        return not_read_problems(root, [root / name for name in files])


READER = "skills/a/scripts/reader.py"


@pytest.mark.parametrize(
    "table",
    [
        "NOT_READ = {'coproc': ''}\n",
        "NOT_READ = {'coproc': '   '}\n",
        "NOT_READ = {'coproc': 1}\n",
        "NOT_READ = {1: 'a reason'}\n",
        "NOT_READ = ['coproc']\n",
        "NOT_READ = 'coproc'\n",
        "NOT_READ = {'coproc': REASON}\n",
        "NOT_READ: dict[str, str] = {'coproc': ''}\n",
    ],
)
def test_a_not_read_table_that_does_not_give_each_entry_its_reason_fails(table: str) -> None:
    for name in (READER, "tools/reader.py", "deployer/reader.py", ".claude/skills/a/scripts/reader.py"):
        assert [problem.split(" NOT_READ")[0] for problem in fixture_not_read({name: table})] == [name], (name, table)
    assert [problem.split(" NOT_READ")[0] for problem in fixture_not_read({"tests/validation/policy.py": table})] == [
        "tests/validation/policy.py"
    ]


@pytest.mark.parametrize(
    "table",
    [
        "NOT_READ = {'coproc': 'runs its command in the background'}\n",
        "NOT_READ: dict[str, str] = {}\n",
        "NOT_READ = {}\n",
        "CATALOGUE = ['coproc']\n",
        "def f() -> None:\n    NOT_READ = 'a local name is no table'\n",
    ],
)
def test_a_not_read_table_giving_each_entry_its_reason_or_none_passes(table: str) -> None:
    assert fixture_not_read({READER: table}) == []


def test_a_suite_a_fault_driver_and_a_fixture_name_their_own_not_read() -> None:
    text = "NOT_READ = 'a reason a fault driver shares'\n"
    files = {
        "skills/a/scripts/test_reader.py": text,
        "tests/malformed_input/faults_x.py": text,
        "tests/tools/agreement.py": text,
        "tests/fixtures/repo/skills/a/scripts/reader.py": text,
        "skills/a/scripts/reader.sh": text,
    }
    assert fixture_not_read(files) == []


TABLE = "NOT_READ: dict[str, str] = {'coproc': 'a reason'}\n"


@pytest.mark.parametrize(
    "write",
    [
        "NOT_READ['fi'] = ''\n",
        "NOT_READ['fi'] = 'a reason'\n",
        "NOT_READ.update({'fi': ''})\n",
        "NOT_READ.setdefault('fi', '')\n",
        "NOT_READ |= {'fi': ''}\n",
        "NOT_READ = {'fi': ''}\n",
        "NOT_READ: dict[str, str] = {}\n",
        "del NOT_READ['coproc']\n",
        "NOT_READ.pop('coproc')\n",
        "if True:\n    NOT_READ = {}\n",
        "def add() -> None:\n    NOT_READ['fi'] = ''\n",
        "def add() -> None:\n    NOT_READ.update(fi='')\n",
        "def rebind() -> None:\n    global NOT_READ\n    NOT_READ = {}\n",
        "import reader\nreader.NOT_READ['fi'] = ''\n",
        "import reader\nreader.NOT_READ = {}\n",
        "(NOT_READ := {})\n",
        "def outer() -> None:\n    def inner() -> None:\n        NOT_READ['fi'] = ''\n",
        "class Reader:\n    NOT_READ['fi'] = ''\n",
        "class Reader:\n    NOT_READ.update(fi='')\n",
        "class Reader:\n    NOT_READ = {}\n\n    def add(self) -> None:\n        NOT_READ['fi'] = ''\n",
        "class Reader:\n    NOT_READ = {}\n    add = lambda self: NOT_READ.update(fi='')\n",
    ],
)
def test_a_write_to_a_not_read_table_after_its_own_assignment_fails(write: str) -> None:
    problems = fixture_not_read({READER: TABLE + write})
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{READER}:"), problems
    assert "writes NOT_READ after the assignment" in problems[0], problems


@pytest.mark.parametrize(
    "use",
    [
        "REASON = NOT_READ['coproc']\n",
        "REASON = NOT_READ.get('coproc')\n",
        "WORDS = sorted(NOT_READ.keys()) + [word for word, _ in NOT_READ.items()]\n",
        "def known(word: str) -> bool:\n    return word in NOT_READ\n",
        "def local() -> dict[str, str]:\n    NOT_READ = {}\n    NOT_READ['x'] = ''\n    return NOT_READ\n",
        "class Reader:\n    NOT_READ = {'x': ''}\n",
        "class Reader:\n    def add(self) -> None:\n        NOT_READ = {}\n        NOT_READ['x'] = ''\n",
        "def add(NOT_READ: dict[str, str]) -> None:\n    NOT_READ['x'] = ''\n",
        "def add(NOT_READ: dict[str, str], /) -> None:\n    NOT_READ.update(x='')\n",
        "def add(*, NOT_READ: dict[str, str]) -> None:\n    NOT_READ['x'] = ''\n",
        "def add(*NOT_READ: dict[str, str]) -> None:\n    del NOT_READ[0]\n",
        "def add(**NOT_READ: str) -> None:\n    NOT_READ['x'] = ''\n",
        "ADD = lambda NOT_READ: NOT_READ.update(x='')\n",
        "class Reader:\n    def add(self, NOT_READ: dict[str, str]) -> None:\n        NOT_READ['x'] = ''\n",
        "REASONS = list(NOT_READ.values()) + [NOT_READ.copy()]\n",
        "KNOWN = NOT_READ.__contains__('coproc') and NOT_READ.__getitem__('coproc') and NOT_READ.__len__()\n",
        "COPY = dict(NOT_READ)\n",
    ],
)
def test_a_not_read_table_read_but_never_written_after_its_assignment_passes(use: str) -> None:
    assert fixture_not_read({READER: TABLE + use}) == []


def test_a_module_whose_table_cannot_be_read_names_the_module_never_passes_empty() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, {"tools/broken.py": "NOT_READ = {'x':\n"})
        (root / "skills" / "a" / "scripts").mkdir(parents=True)
        (root / READER).write_bytes(b"NOT_READ = {'x': '\xff'}\n")
        problems = not_read_problems(root, [root / "tools" / "broken.py", root / READER])
    assert [problem.split(" ")[0] for problem in problems] == [READER, "tools/broken.py"], problems


@pytest.mark.parametrize("before", ["WORDS = ['coproc']\n", "class Words:\n    pass\n\n\nWords.first = 'coproc'\n"])
def test_a_not_read_table_after_another_module_level_assignment_is_read_from_its_own(before: str) -> None:
    assert fixture_not_read({READER: before + TABLE}) == []


def test_each_not_read_problem_names_its_module_its_line_and_what_to_do() -> None:
    assert fixture_not_read(
        {READER: TABLE + "\nNOT_READ['fi'] = ''\n", "tools/reader.py": "NOT_READ = {'x': ''}\n"}
    ) == [
        f"{READER}:3 writes NOT_READ after the assignment validation reads its reasons from; add each entry to that "
        "literal instead",
        "tools/reader.py NOT_READ must map each catalogue entry the module leaves unread to its reason, as a literal "
        "dictionary of non-empty texts",
    ]


def fixture_catalogue_tests(files: dict[str, str]) -> list[str]:
    """The CATALOGUE_TESTS problems of a fixture tree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, files)
        return catalogue_test_problems(root, [root / name for name in files])


# A suite the fixtures' CATALOGUE_TESTS name: a top-level test, a test method, and a function nested in a test; and a
# file of the same text whose name is no suite's.
CATALOGUE_SUITE = (
    "import unittest\n\n\ndef test_words():\n    def test_nested():\n        pass\n\n\n"
    "class Catalogue(unittest.TestCase):\n    def test_options(self):\n        pass\n"
)
SUITES = {"tests/tools/test_reader.py": CATALOGUE_SUITE, "skills/a/scripts/helper.py": CATALOGUE_SUITE}


@pytest.mark.parametrize(
    "source",
    [
        "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\n",
        "CATALOGUE_TESTS: dict[str, str] = {'options': 'tests/tools/test_reader.py::Catalogue.test_options'}\n",
        "NOT_READ = {'coproc': 'why'}\nCATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\n",
        "NOT_READ: dict[str, str] = {}\nCATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\n",
        # A module that decides no catalogue declares none.
        "WORDS = ['coproc']\n",
        "CATALOGUE_TESTS = {}\n",
    ],
)
def test_a_catalogue_test_table_naming_a_test_its_suite_defines_passes(source: str) -> None:
    for name in (READER, "tools/reader.py", "deployer/reader.py", ".claude/skills/a/scripts/reader.py"):
        assert fixture_catalogue_tests({name: source, **SUITES}) == [], (name, source)


@pytest.mark.parametrize(
    ("source", "fault"),
    [
        ("CATALOGUE_TESTS = {'words': ''}\n", "must map each catalogue"),
        ("CATALOGUE_TESTS = {'words': 1}\n", "must map each catalogue"),
        ("CATALOGUE_TESTS = ['tests/tools/test_reader.py::test_words']\n", "must map each catalogue"),
        ("CATALOGUE_TESTS = {'words': TEST}\n", "must map each catalogue"),
        ("NOT_READ = {'coproc': 'why'}\n", "binds NOT_READ but names no catalogue test"),
        ("NOT_READ: dict[str, str] = {}\nCATALOGUE_TESTS = {}\n", "binds NOT_READ but names no catalogue test"),
        ("CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py'}\n", "which is not <suite path>::<test>"),
        ("CATALOGUE_TESTS = {'words': 'test_words'}\n", "which is not <suite path>::<test>"),
        (
            "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::A.B.test_x'}\n",
            "which is not <suite path>::<test>",
        ),
        ("CATALOGUE_TESTS = {'words': 'tests/tools/test_gone.py::test_words'}\n", "which is no tracked suite"),
        ("CATALOGUE_TESTS = {'words': 'skills/a/scripts/helper.py::test_words'}\n", "which is no tracked suite"),
        ("CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_gone'}\n", "which its suite does not define"),
        ("CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_nested'}\n", "which its suite does not define"),
        ("CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::Catalogue'}\n", "which its suite does not define"),
        (
            "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::Catalogue.test_gone'}\n",
            "which its suite does not define",
        ),
        (
            "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\nCATALOGUE_TESTS['x'] = 'y'\n",
            "writes CATALOGUE_TESTS after the assignment",
        ),
        (
            "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\n"
            "def add() -> None:\n    CATALOGUE_TESTS.update(x='y')\n",
            "writes CATALOGUE_TESTS after the assignment",
        ),
    ],
)
def test_a_catalogue_test_table_that_names_no_test_its_suite_defines_fails(source: str, fault: str) -> None:
    for name in (READER, "tools/reader.py", "tests/validation/policy.py"):
        problems = fixture_catalogue_tests({name: source, **SUITES})
        assert len(problems) == 1, (name, problems)
        assert problems[0].startswith(name), (name, problems)
        assert fault in problems[0], (name, problems)


CATALOGUE_TABLE = "CATALOGUE_TESTS = {'words': 'tests/tools/test_reader.py::test_words'}\n"


@pytest.mark.parametrize(
    "write",
    [
        "CATALOGUE_TESTS = {}\n",
        "def rebind() -> None:\n    global CATALOGUE_TESTS\n    CATALOGUE_TESTS = {}\n",
        "def add() -> None:\n    global CATALOGUE_TESTS\n    CATALOGUE_TESTS['x'] = 'y'\n",
        "class Reader:\n    CATALOGUE_TESTS['x'] = 'y'\n",
        "import reader\nreader.CATALOGUE_TESTS = {}\n",
    ],
)
def test_a_rebinding_of_catalogue_tests_after_its_assignment_fails_as_one_of_not_read_does(write: str) -> None:
    for table, text in (
        ("CATALOGUE_TESTS", CATALOGUE_TABLE + write),
        ("NOT_READ", TABLE + write.replace("CATALOGUE_TESTS", "NOT_READ")),
    ):
        problems = (
            fixture_catalogue_tests({READER: text, **SUITES})
            if table == "CATALOGUE_TESTS"
            else fixture_not_read({READER: text})
        )
        assert [problem for problem in problems if f"writes {table} after the assignment" in problem] == problems
        assert len(problems) == 1, (table, problems)


@pytest.mark.parametrize(
    "use",
    [
        "def local() -> None:\n    CATALOGUE_TESTS = {}\n    CATALOGUE_TESTS['x'] = 'y'\n",
        "def add(CATALOGUE_TESTS: dict[str, str]) -> None:\n    CATALOGUE_TESTS['x'] = 'y'\n",
        "class Reader:\n    def add(self) -> None:\n        CATALOGUE_TESTS = {}\n        CATALOGUE_TESTS['x'] = 'y'\n",
    ],
)
def test_a_local_name_spelled_catalogue_tests_is_no_write_to_the_table(use: str) -> None:
    assert fixture_catalogue_tests({READER: CATALOGUE_TABLE + use, **SUITES}) == []


def test_a_suite_a_fault_driver_and_a_fixture_declare_no_catalogue_tests() -> None:
    text = "NOT_READ = 'a reason a fault driver shares'\nCATALOGUE_TESTS = {'words': 'gone.py::test_x'}\n"
    files = {
        "skills/a/scripts/test_reader.py": text,
        "tests/malformed_input/faults_x.py": text,
        "tests/tools/agreement.py": text,
        "tests/fixtures/repo/skills/a/scripts/reader.py": text,
    }
    assert fixture_catalogue_tests(files) == []


def test_each_problem_names_the_module_the_reference_why_and_what_to_do() -> None:
    files = {
        "tools/reader.py": "NOT_READ = {'x': 'why'}\nCATALOGUE_TESTS = {'words': 'tests/test_reader.py::test_x'}\n",
        "tests/test_reader.py": "def test_y():\n    pass\n",
    }
    assert fixture_catalogue_tests(files) == [
        "tools/reader.py CATALOGUE_TESTS names tests/test_reader.py::test_x for words, which its suite does not "
        "define; name the test that decides every entry of that catalogue",
    ]


def test_a_suite_that_does_not_parse_or_cannot_be_read_is_named_with_the_reason() -> None:
    reader = "CATALOGUE_TESTS = {'words': 'tests/test_broken.py::test_x'}\n"
    [problem] = fixture_catalogue_tests({"tools/reader.py": reader, "tests/test_broken.py": "def test_x(:\n"})
    assert problem.startswith("tools/reader.py CATALOGUE_TESTS names tests/test_broken.py::test_x for words, whose ")
    assert "whose suite does not parse (" in problem
    assert "line 1" in problem
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repository with spaces"
        write_fixture_tree(root, {"tools/reader.py": reader})
        (root / "tests").mkdir()
        (root / "tests" / "test_broken.py").write_bytes(b"def test_x():\n    return '\xff'\n")
        [problem] = catalogue_test_problems(root, [root / "tools" / "reader.py", root / "tests" / "test_broken.py"])
    assert "whose suite cannot be read (" in problem
    assert "utf-8" in problem

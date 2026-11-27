"""Regression tests for catalogue_tests.py, the one reading of a module's CATALOGUE_TESTS: the catalogues it decides,
each with the test that holds every entry of it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalogue_tests
import python_reader


def declared(source: str) -> dict[str, str] | None:
    return catalogue_tests.declared(python_reader.parse(source))


def test_the_table_s_name_is_pinned() -> None:
    assert catalogue_tests.TABLE == "CATALOGUE_TESTS"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "CATALOGUE_TESTS = {'reserved words': 'tests/test_a.py::test_x'}\n",
            {"reserved words": "tests/test_a.py::test_x"},
        ),
        ("CATALOGUE_TESTS: dict[str, str] = {'words': 't/test_a.py::C.test_x'}\n", {"words": "t/test_a.py::C.test_x"}),
        ("CATALOGUE_TESTS = {}\n", {}),
        # A module that binds no table, or binds the name only inside a function, declares none.
        ("NOT_READ = {'coproc': 'why'}\n", {}),
        ("def f():\n    CATALOGUE_TESTS = {'a': 'b'}\n", {}),
    ],
)
def test_a_literal_dictionary_of_texts_at_module_level_is_the_declaration(
    source: str, expected: dict[str, str]
) -> None:
    assert declared(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "CATALOGUE_TESTS = {'words': ''}\n",
        "CATALOGUE_TESTS = {'words': '   '}\n",
        "CATALOGUE_TESTS = {'': 'tests/test_a.py::test_x'}\n",
        "CATALOGUE_TESTS = {'words': 1}\n",
        "CATALOGUE_TESTS = {1: 'tests/test_a.py::test_x'}\n",
        "CATALOGUE_TESTS = ['tests/test_a.py::test_x']\n",
        "CATALOGUE_TESTS = {'words': TEST}\n",
    ],
)
def test_any_other_value_is_no_declaration(source: str) -> None:
    assert declared(source) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("tests/tools/test_a.py::test_x", ("tests/tools/test_a.py", "test_x")),
        ("tests/tools/test_a.py::Catalogue.test_x", ("tests/tools/test_a.py", "Catalogue.test_x")),
        ("skills/a b/scripts/test_a.py::test_x", ("skills/a b/scripts/test_a.py", "test_x")),
    ],
)
def test_a_reference_is_a_suite_path_and_a_test_s_qualified_name(value: str, expected: tuple[str, str]) -> None:
    assert catalogue_tests.reference(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "tests/tools/test_a.py",
        "test_x",
        "::test_x",
        "tests/tools/test_a.py::",
        "tests/tools/test_a.py::test_x::test_y",
        "tests/tools/test_a.py::A.B.test_x",
        "tests/tools/test_a.py::test_x[case]",
        "tests/tools/test_a.py::1test",
        "/abs/test_a.py::test_x",
        "tests/../test_a.py::test_x",
        "tests\\tools\\test_a.py::test_x",
    ],
)
def test_any_other_reference_is_refused(value: str) -> None:
    assert catalogue_tests.reference(value) is None


SUITE = """
import unittest


def test_function():
    def test_nested():
        pass


async def test_waits():
    pass


class Catalogue(unittest.TestCase):
    def test_method(self):
        pass

    class Inner:
        def test_inner(self):
            pass


test_assigned = test_function
"""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test_function", True),
        ("test_waits", True),
        ("Catalogue.test_method", True),
        ("Catalogue", False),
        ("test_nested", False),
        ("test_function.test_nested", False),
        ("Catalogue.Inner.test_inner", False),
        ("Catalogue.test_missing", False),
        ("test_missing", False),
        ("test_assigned", False),
    ],
)
def test_a_suite_defines_a_top_level_function_or_a_method_of_a_top_level_class(name: str, expected: bool) -> None:
    assert catalogue_tests.defines(python_reader.parse(SUITE), name) is expected

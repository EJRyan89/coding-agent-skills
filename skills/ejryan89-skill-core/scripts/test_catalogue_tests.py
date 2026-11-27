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
import unittest as ut
from unittest import IsolatedAsyncioTestCase
from unittest import TestCase as Base


def test_function():
    def test_nested():
        pass


async def test_waits():
    pass


def helper():
    pass


class Catalogue(unittest.TestCase):
    def test_method(self):
        pass

    def setUp(self):
        pass

    def helper(self):
        pass

    class Inner:
        def test_inner(self):
            pass


class Aliased(ut.TestCase):
    def test_aliased(self):
        pass


class Imported(Base):
    def test_imported(self):
        pass


class Waits(IsolatedAsyncioTestCase):
    async def test_waits(self):
        pass


class LocalCase(unittest.TestCase):
    def __init__(self, name="runTest"):
        super().__init__(name)


class Inherited(LocalCase):
    def test_inherited(self):
        pass


class Mixed(Catalogue, object):
    def test_mixed(self):
        pass


class TestPlain:
    def test_plain(self):
        pass


class TestObject(object):
    def test_object(self):
        pass


class TestBased(TestPlain):
    def test_based(self):
        pass


class Helper:
    def test_guard(self):
        pass


class TestInit:
    def __init__(self):
        pass

    def test_guard(self):
        pass


class TestNew:
    def __new__(cls):
        return super().__new__(cls)

    def test_guard(self):
        pass


class Constructs:
    def __init__(self):
        pass


class TestInherits(Constructs):
    def test_guard(self):
        pass


class TestCaseInit(unittest.TestCase):
    def __init__(self, name="runTest"):
        super().__init__(name)

    def test_guard(self):
        pass


class TestOff:
    __test__ = False

    def test_guard(self):
        pass


class CaseOff(unittest.TestCase):
    __test__ = False

    def test_guard(self):
        pass


class OffBase:
    __test__ = False


class TestOffBase(OffBase):
    def test_guard(self):
        pass


class TestAnnotatedOff:
    __test__: bool = False

    def test_guard(self):
        pass


test_assigned = test_function
"""

# Each name refusal decides in SUITE, with why it refuses the name, or None when pytest, at its defaults, collects a
# test of that name: the forms tests/validation/test_workarounds.py holds to what pytest itself collects from SUITE.
NO_TEST = catalogue_tests.NO_TEST
UNCOLLECTED = catalogue_tests.UNCOLLECTED
DECIDED: list[tuple[str, str | None]] = [
    ("test_function", None),
    ("test_waits", None),
    ("Catalogue.test_method", None),
    # A unittest.TestCase subclass of any name, through any import of it, or through the suite's own subclass of it,
    # even one that defines __init__.
    ("Aliased.test_aliased", None),
    ("Imported.test_imported", None),
    ("Waits.test_waits", None),
    ("Inherited.test_inherited", None),
    ("Mixed.test_mixed", None),
    ("TestCaseInit.test_guard", None),
    # A class whose name starts with `Test` and whose lineage, the suite's own classes, defines no __init__ or __new__.
    ("TestPlain.test_plain", None),
    ("TestObject.test_object", None),
    ("TestBased.test_based", None),
    ("Catalogue", NO_TEST),
    ("test_nested", NO_TEST),
    ("test_function.test_nested", NO_TEST),
    ("Catalogue.Inner.test_inner", NO_TEST),
    ("Catalogue.test_missing", NO_TEST),
    ("test_missing", NO_TEST),
    # A function or method pytest and unittest do not collect as a test, its name not starting with `test`.
    ("helper", NO_TEST),
    ("Catalogue.helper", NO_TEST),
    ("Catalogue.setUp", NO_TEST),
    # A method of a class pytest never collects: a plain class of another name, a `Test` class that defines __init__
    # or __new__ or inherits one, and a class that sets __test__, by an assignment or an annotated one, or inherits it
    # set.
    ("Helper.test_guard", UNCOLLECTED),
    ("TestInit.test_guard", UNCOLLECTED),
    ("TestNew.test_guard", UNCOLLECTED),
    ("TestInherits.test_guard", UNCOLLECTED),
    ("TestOff.test_guard", UNCOLLECTED),
    ("CaseOff.test_guard", UNCOLLECTED),
    ("TestOffBase.test_guard", UNCOLLECTED),
    ("TestAnnotatedOff.test_guard", UNCOLLECTED),
]
# The tests pytest collects from SUITE that refusal refuses as NO_TEST, which a reference cannot name: one bound by
# assignment, and a method a class inherits, since the method is read in the class's own body alone.
STRICTER = ["test_assigned", "Mixed.test_method", "TestBased.test_plain"]

# The classes whose collection the suite alone cannot settle, which refusal refuses as UNSETTLED though pytest may
# collect them: a base imported from another module, a base the suite defines only later, a metaclass, and a decorator.
UNSETTLED_SUITE = """
import unittest

from support import SupportCase


class Elsewhere(SupportCase):
    def test_guard(self):
        pass


class TestElsewhere(SupportCase):
    def test_guard(self):
        pass


class TestLater(Afterwards):
    def test_guard(self):
        pass


class Afterwards(unittest.TestCase):
    pass


class TestMeta(metaclass=type):
    def test_guard(self):
        pass


def decorate(cls):
    return cls


@decorate
class TestDecorated:
    def test_guard(self):
        pass


class TestOnElsewhere(TestElsewhere):
    def test_guard(self):
        pass


class TestOnMeta(TestMeta):
    def test_guard(self):
        pass
"""
UNSETTLED = [
    "Elsewhere.test_guard",
    "TestElsewhere.test_guard",
    "TestLater.test_guard",
    "TestMeta.test_guard",
    "TestDecorated.test_guard",
    # A class whose base is the suite's own, but whose base's lineage the suite does not settle.
    "TestOnElsewhere.test_guard",
    "TestOnMeta.test_guard",
]


@pytest.mark.parametrize(("name", "expected"), DECIDED)
def test_a_suite_defines_a_test_pytest_collects(name: str, expected: str | None) -> None:
    assert catalogue_tests.refusal(python_reader.parse(SUITE), name) == expected


@pytest.mark.parametrize("name", UNSETTLED)
def test_a_method_of_a_class_the_suite_alone_does_not_settle_is_refused(name: str) -> None:
    assert catalogue_tests.refusal(python_reader.parse(UNSETTLED_SUITE), name) == catalogue_tests.UNSETTLED


@pytest.mark.parametrize("name", STRICTER)
def test_a_test_bound_by_assignment_or_inherited_is_refused(name: str) -> None:
    assert catalogue_tests.refusal(python_reader.parse(SUITE), name) == catalogue_tests.NO_TEST


def test_each_refusal_names_its_cause() -> None:
    assert catalogue_tests.NO_TEST == "defines no test of that name"
    assert "a class pytest does not collect" in catalogue_tests.UNCOLLECTED
    assert "a base imported from another module" in catalogue_tests.UNSETTLED

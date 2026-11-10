"""Regression tests for the one reader of a module-level assignment to a single name."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from module_assignments import assigned_names, sole_assignment


@pytest.mark.parametrize(
    ("source", "name", "value"),
    [
        pytest.param("TABLE = {'a': 1}", "TABLE", "{'a': 1}", id="a plain assignment"),
        pytest.param("TABLE: dict[str, int] = {}", "TABLE", "{}", id="an annotated assignment"),
    ],
)
def test_a_statement_assigning_one_name_gives_the_name_and_its_value(source: str, name: str, value: str) -> None:
    found = sole_assignment(ast.parse(source).body[0])
    assert found is not None
    target, expression = found
    assert (target.id, ast.unparse(expression)) == (name, value)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("A = B = 1", id="a chained assignment"),
        pytest.param("A, B = 1, 2", id="an unpacking assignment"),
        pytest.param("A: int", id="an annotation with no value"),
        pytest.param("A += 1", id="an augmented assignment"),
        pytest.param("x.A = 1", id="an attribute target"),
        pytest.param("x['A'] = 1", id="a subscript target"),
        pytest.param("if True:\n    A = 1", id="a compound statement"),
        pytest.param("print(1)", id="an expression"),
    ],
)
def test_any_other_statement_assigns_no_one_name(source: str) -> None:
    assert sole_assignment(ast.parse(source).body[0]) is None


@pytest.mark.parametrize(
    ("source", "names"),
    [
        pytest.param("A = 1", ["A"], id="a plain assignment"),
        pytest.param("A = B = 1", ["A", "B"], id="a chained assignment"),
        pytest.param("A, [B, *C] = 1, [2, 3]", ["A", "B", "C"], id="an unpacking assignment"),
        pytest.param("A: int", ["A"], id="an annotation with no value"),
        pytest.param("A += 1", ["A"], id="an augmented assignment"),
        pytest.param("x.A = x[B] = 1", [], id="attribute and subscript targets"),
        pytest.param("if True:\n    A = 1", [], id="a compound statement"),
    ],
)
def test_every_name_an_assignment_binds_is_named(source: str, names: list[str]) -> None:
    assert assigned_names(ast.parse(source).body[0]) == names

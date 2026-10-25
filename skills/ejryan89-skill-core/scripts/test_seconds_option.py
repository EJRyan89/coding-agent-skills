"""Regression tests for the one command-line type for a time limit in seconds."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seconds_option import positive_seconds


@pytest.mark.parametrize(("value", "expected"), [("30", 30.0), ("0.5", 0.5), ("1e-9", 1e-9), (" 2 ", 2.0)])
def test_a_finite_number_above_zero_is_read_as_a_float(value: str, expected: float) -> None:
    assert positive_seconds(value) == expected


@pytest.mark.parametrize(
    "value", ["nan", "NaN", "-nan", "inf", "Infinity", "-inf", "1e400", "0", "-0", "0.0", "-1", "-0.5", "", "ten"]
)
def test_anything_else_is_refused_naming_the_value(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError) as refused:
        positive_seconds(value)
    # The message is pinned here once against a literal; the sites' suites take it from a refusal.
    assert str(refused.value) == f"must be a positive number of seconds, not {value}"


def test_argparse_ends_a_refused_value_with_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser(prog="tool")
    parser.add_argument("--timeout", type=positive_seconds, default=30.0)
    assert parser.parse_args([]).timeout == 30.0
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["--timeout", "nan"])
    assert exited.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        "tool: error: argument --timeout: must be a positive number of seconds, not nan"
    )

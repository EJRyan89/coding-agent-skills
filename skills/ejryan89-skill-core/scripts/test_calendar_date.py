"""Regression suite for calendar_date: a YYYY-MM-DD date in ASCII digits that the calendar has, and nothing else."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("2026-11-09", date(2026, 11, 9), id="a-day"),
        pytest.param("2028-02-29", date(2028, 2, 29), id="a-leap-day"),
        pytest.param("0001-01-01", date(1, 1, 1), id="the-first-day"),
    ],
)
def test_a_date_written_yyyy_mm_dd_is_read(text: str, expected: date) -> None:
    assert calendar_date.parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("\u0662\u0660\u0662\u0666-\u0661\u0661-\u0660\u0669", id="arabic-indic-digits"),
        pytest.param("\uff12\uff10\uff12\uff16-11-09", id="fullwidth-digits"),
        pytest.param("2026-13-45", id="no-such-month"),
        pytest.param("2026-02-30", id="no-such-day"),
        pytest.param("2027-02-29", id="no-leap-day"),
        pytest.param("0000-01-01", id="year-zero"),
        pytest.param("20261109", id="basic-form"),
        pytest.param("2026-W45-1", id="week-date"),
        pytest.param("2026-11-09T00:00", id="a-time-after-it"),
        pytest.param(" 2026-11-09", id="leading-space"),
        pytest.param("2026-11-09\n", id="trailing-newline"),
        pytest.param("2026-1-09", id="one-digit-month"),
        pytest.param("", id="empty"),
    ],
)
def test_any_other_text_is_no_date(text: str) -> None:
    assert calendar_date.parse(text) is None

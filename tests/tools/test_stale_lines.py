"""The one report of a generated file out of step with its source, shared by the tools that regenerate one."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import stale_lines


def test_each_problem_prints_on_a_stale_line_in_order_and_exits_1() -> None:
    # A pinned contract value: change-skill's SKILL.md tells a contributor to read these lines by the word STALE.
    assert stale_lines.STALE == "STALE"
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = stale_lines.report(["b.md does not exist", "a.md is stale; run it"])
    assert (code, printed.getvalue()) == (1, "STALE b.md does not exist\nSTALE a.md is stale; run it\n")


def test_nothing_found_prints_nothing_and_exits_0() -> None:
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = stale_lines.report([])
    assert (code, printed.getvalue()) == (0, "")

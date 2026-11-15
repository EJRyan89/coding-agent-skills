"""Regression tests for where ejryan89-code-review-core's readers end a line: a log the reviewer guard writes, a
review record's prose, and a pull request body each split where the program that wrote or renders it ends a line,
never at a line separator."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import review_dispatch
import review_records
import review_runtime


def test_a_guard_log_line_is_read_whole_through_a_line_separator_a_path_holds(tmp_path: Path) -> None:
    # The guard writes one JSON string to a line, ended by LF; a path it records may hold U+2028, unescaped.
    source = tmp_path / "source"
    source.mkdir()
    (source / "a\u2028b.md").write_bytes(b"12345")
    log = tmp_path / "reads.log"
    log.write_bytes((json.dumps("a\u2028b.md", ensure_ascii=False) + "\r\n").encode())
    sizes, searches = review_dispatch._read_log(log, source)
    assert (list(sizes.values()), searches) == ([5], 0)


def test_a_record_s_prose_quote_and_evidence_break_where_markdown_ends_a_line() -> None:
    # Markdown ends a line at CR, LF, or CRLF, never at a line separator, which stays in its line.
    assert review_records._prose("a\u2028b\rc") == "a\u2028b<br>\nc"
    assert review_records._quote("a\u2028b") == ["> a\u2028b"]
    assert review_records._evidence("a\u2028b\r\nc") == "`a b`<br>`c`"
    finding: Any = {
        "title": "T",
        "path": "a.py",
        "line": 3,
        "evidence": "e",
        "source": "diff",
        "body": "b",
        "category": "Correctness",
    }
    block = review_records._finding_block(finding, "F1", status=["seen a\u2028b"])
    assert "> seen a\u2028b" in block


def test_a_body_s_lines_end_where_markdown_ends_one() -> None:
    # A lone CR ends a line as LF does; a line separator ends none.
    assert review_runtime.body_lines("a\rb\u2028c\r\nd") == ["a", "b\u2028c", "d"]

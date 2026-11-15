"""Fixture tests for where tests/validation/skill_scripts.py's readers end a line: each splits a file as the program
that reads it does, Bash at LF alone, PowerShell, CommonMark, and Python at CR, LF, and CRLF, and none at a line
separator or a form feed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import skill_scripts
from skill_scripts import gh_filter_problems, script_contract_problems
from validation_support import write_fixture_tree


def test_a_bash_script_s_exit_is_numbered_where_bash_ends_its_line(tmp_path: Path) -> None:
    # Bash ends a line at LF alone, so a line separator inside a quoted word leaves the exit on line 2.
    write_fixture_tree(
        tmp_path,
        {"skills/alpha/SKILL.md": "# alpha\n", "skills/alpha/scripts/tool.sh": "echo 'a\u2028b'\nexit 3\n"},
        newline="\n",
    )
    doc = '"Script results" in docs/adding-a-skill.md'
    assert script_contract_problems(tmp_path) == [
        f"skills/alpha/scripts/tool.sh:2 exits 3; scripts exit only 0, 1, or 2; see {doc}"
    ]


def test_a_script_s_code_lines_end_where_its_shell_ends_one() -> None:
    # Bash ends a line at LF alone, reading a CR as part of it; PowerShell at CR, LF, or CRLF; neither at U+2028.
    assert skill_scripts._code_lines("a\u2028b\nc\r\nd\n") == [(1, "a\u2028b"), (2, "c\r"), (3, "d")]
    assert skill_scripts._code_lines("a\u2028b\rc\r\nd\n", "`") == [(1, "a\u2028b"), (2, "c"), (3, "d")]


def test_a_fence_is_found_where_commonmark_ends_a_line() -> None:
    # A line separator ends no line, so the text before it and the opener after it are one line of prose.
    assert skill_scripts._fences("intro\u2028```bash\necho no\n", "shell") == []
    assert skill_scripts._fences("intro\r```bash\recho yes\r```\r", "shell") == ["echo yes"]


def test_a_sanction_s_lines_are_numbered_as_python_numbers_them() -> None:
    # Python ends no line at a form feed, so the sanction is line 2 and its reason names no script that runs.
    source = 'X = "\x0c"\nDUPLICATION_ALLOWED = {"a": "see /beta/scripts/other.py"}\nUSED = "/beta/scripts/used.py"\n'
    assert skill_scripts._references(source, True) == frozenset({"used"})


def test_a_bash_script_s_lone_cr_ends_no_line(tmp_path: Path) -> None:
    # Bash reads a CR as part of its line, so the exit after it is in the comment, and the backslash before a CRLF
    # continues nothing, so the --jq after it is a command of its own, not gh's.
    write_fixture_tree(
        tmp_path,
        {
            "skills/alpha/SKILL.md": "# alpha\n",
            "skills/alpha/scripts/tool.sh": "#!/usr/bin/env bash\n# a note\rexit 3\nexit 0\n",
            "skills/alpha/scripts/query.sh": "gh pr list \\\r\n  --jq .x\n",
        },
        # Written as each byte stands, as a checkout holds an LF-ended script, so each CR is the test's own.
        newline="\n",
    )
    assert script_contract_problems(tmp_path) == []
    assert gh_filter_problems(tmp_path) == []

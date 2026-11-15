"""Regression tests for how powershell_commands.read names a pwsh that listed no commands."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import powershell_commands
from bounded_process import Finished
from flat_text import REASON_CHARACTERS
from powershell_commands import PowerShellUnread

PREFIX = "pwsh could not list the commands: "


def unread_reason(finished: Finished) -> str:
    """The reason `read` refuses one source with when pwsh leaves `finished`, run with no pwsh started."""
    with (
        mock.patch.object(powershell_commands, "find_executable", return_value="pwsh"),
        mock.patch.object(powershell_commands, "run_bounded", return_value=finished),
        pytest.raises(PowerShellUnread) as raised,
    ):
        powershell_commands.read(["git status"])
    message = str(raised.value)
    assert message.startswith(PREFIX), message
    return message.removeprefix(PREFIX)


@pytest.mark.parametrize(
    ("finished", "reason"),
    [
        pytest.param(
            Finished(1, b"", b"ParserError:\r\nLine |\r\n   1 |  bad\x1b[0m\r\n     | Missing closing '}'\r\n"),
            "ParserError: Line | 1 | bad [0m | Missing closing '}'",
            id="error-lines",
        ),
        pytest.param(Finished(1, b"", b""), "exit code 1", id="nothing-printed"),
        pytest.param(Finished(3, b"partial\n", b" \r\n\t"), "exit code 3", id="whitespace-only-stderr"),
        pytest.param(Finished(0, b"", b""), "it exited 0 and printed nothing", id="exit-0-nothing"),
        pytest.param(
            Finished(0, b'[{"source": 0}\nnot json\r\n', b""),
            "it exited 0 and its last line is not a list of commands: not json",
            id="exit-0-not-a-list",
        ),
        pytest.param(
            Finished(0, b'{"a": 1}\n', b"WARNING: a module could not load\n"),
            'it exited 0 and its last line is not a list of commands: {"a": 1}; '
            "its stderr: WARNING: a module could not load",
            id="exit-0-with-errors",
        ),
    ],
)
def test_a_pwsh_that_lists_no_commands_is_named_by_its_errors_on_one_line_or_its_exit_code(
    finished: Finished, reason: str
) -> None:
    assert reason == unread_reason(finished)


def test_long_errors_are_cut_to_one_bounded_line() -> None:
    reason = unread_reason(Finished(1, b"", b"Error: " + b"x" * 5000 + b"\n"))
    assert reason.startswith("Error: xxx")
    assert reason.endswith("...")
    assert len(reason) == REASON_CHARACTERS + 3

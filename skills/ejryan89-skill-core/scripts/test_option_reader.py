"""Regression tests for the one reader of a program's option words as getopt reads them."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from option_reader import NO_VALUE, OPTIONAL_VALUE, VALUE, Word, option_refusal, option_table


def test_a_table_is_built_from_getopt_s_spelling() -> None:
    table = option_table("ab:c::", "flag name= maybe=?", slash="L O=")
    assert dict(table.short) == {"a": NO_VALUE, "b": VALUE, "c": OPTIONAL_VALUE}
    assert dict(table.long) == {"--flag": NO_VALUE, "--name": VALUE, "--maybe": OPTIONAL_VALUE}
    assert table.slash == {"L": NO_VALUE, "O": VALUE}
    assert dict(option_table("", "name", single_dash=True).long) == {"-name": NO_VALUE}


XARGS = option_table("0n:", "max-args= null")


@pytest.mark.parametrize(
    ("arguments", "stop", "values"),
    [
        (["-0", "git", "push"], 1, []),
        (["-n", "1", "git"], 2, [("-n", "1")]),
        (["-n1", "git"], 1, [("-n", "1")]),
        (["-0n", "1", "git"], 2, [("-n", "1")]),
        (["--max-args", "2", "git"], 2, [("--max-args", "2")]),
        (["--max-args=2", "git"], 1, [("--max-args", "2")]),
        (["--", "-git"], 1, []),
        ([], 0, []),
    ],
)
def test_a_runner_s_options_end_at_its_command_in_every_spelling(
    arguments: list[Word], stop: int, values: list[tuple[str, Word]]
) -> None:
    reading = option_refusal(XARGS, arguments)
    assert (reading.refused, reading.stop, reading.values) == (None, stop, values)


@pytest.mark.parametrize(
    ("arguments", "refused"),
    [
        (["--max-a", "1", "git"], "--max-a"),
        (["-I", "X", "git"], "-I"),
        (["-0I", "git"], "-0I"),
        (["--null=1"], "--null=1"),
    ],
)
def test_a_word_the_table_does_not_settle_is_refused(arguments: list[Word], refused: str) -> None:
    assert option_refusal(XARGS, arguments).refused == refused


def test_operands_assignments_and_numbered_options_stand_before_the_command() -> None:
    timeout = option_refusal(option_table("s:", "signal=", operands=1), ["-s", "TERM", "5", "git"])
    assert (timeout.stop, timeout.values) == (3, [("-s", "TERM")])
    env = option_refusal(option_table("u:", assignments=True), ["A=1", "-u", "B", "-", "git"])
    assert (env.stop, env.assigned) == (4, [("A", "1")])
    nice = option_refusal(option_table(numbered=re.compile(r"-[+-]?[0-9]+")), ["-5", "git"])
    assert nice.stop == 1


def test_a_value_taking_option_at_the_end_takes_an_empty_value() -> None:
    reading = option_refusal(XARGS, ["-n"])
    assert (reading.refused, reading.stop, reading.values) == (None, 1, [("-n", "")])
    assert option_refusal(XARGS, ["--max-args"]).values == [("--max-args", "")]


def test_a_word_the_text_does_not_settle_ends_the_reading() -> None:
    assert (option_refusal(XARGS, ["-0", None, "git"]).refused, option_refusal(XARGS, ["-0", None]).stop) == (None, 1)


def test_a_permuting_program_is_read_to_its_end_or_its_double_dash() -> None:
    table = option_table("u", "unique", permutes=True)
    assert option_refusal(table, ["a", "-u", "b"]).stop == 3
    assert option_refusal(table, ["a", "--", "-x"]).stop == 1
    assert option_refusal(table, ["a", "-x"]).refused == "-x"


def test_a_windows_build_reads_its_slash_options_in_any_case() -> None:
    table = option_table("r", slash="R O=")
    assert option_refusal(table, ["/r", "/o", "x", "in"], windows_build=True).refused is None
    assert option_refusal(table, ["/T"], windows_build=True).refused == "/T"
    long_s = "/" + chr(0x17F)  # Python's case folding reads it as `S`
    assert option_refusal(table, [long_s], windows_build=True).refused == long_s


def test_tar_s_old_style_first_word_takes_its_values_in_order() -> None:
    table = option_table("tf:C:", first_letters=True, permutes=True)
    assert option_refusal(table, ["tf", "a.tar"]).values == [("-f", "a.tar")]
    assert option_refusal(table, ["xf", "a.tar"]).refused == "xf"

"""Regression tests for the one reader of a GitHub login."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_logins import is_login


class LoginTests(unittest.TestCase):
    def test_a_login_longer_than_thirty_nine_characters_is_refused(self) -> None:
        self.assertFalse(is_login("a" * 40))
        self.assertFalse(is_login("a" * 35 + "_octo"))


@pytest.mark.parametrize(
    "login", ["a", "octocat", "Octo-Cat", "a-b-c", "x1-2y", pytest.param("a" * 39, id="thirty-nine-letters")]
)
def test_letters_digits_and_single_inner_hyphens_are_a_login(login: str) -> None:
    assert is_login(login)


@pytest.mark.parametrize(
    "login", ["mona_octo", "mona-lisa_octo", "m_o", pytest.param("a" * 34 + "_octo", id="thirty-four-letters_octo")]
)
def test_an_enterprise_managed_login_carries_its_short_code_after_one_underscore(login: str) -> None:
    assert is_login(login)


@pytest.mark.parametrize(
    "login", ["-octocat", "octocat-", "octo--cat", "_octocat", "octocat_", "mona_octo_x", "mona_-octo"]
)
def test_a_misplaced_hyphen_or_underscore_is_refused(login: str) -> None:
    assert not is_login(login)


@pytest.mark.parametrize(
    "login",
    [
        pytest.param("", id="empty"),
        "octo cat",
        "octo:cat",
        "octo'cat",
        'octo"cat',
        "octo/cat",
        pytest.param("octocat\n", id="octocat-newline"),
        "octo.cat",
        pytest.param("ö", id="a-letter-outside-ascii"),
    ],
)
def test_a_character_a_query_or_a_command_line_reads_apart_is_refused(login: str) -> None:
    assert not is_login(login)

"""Regression tests for the one reader of a GitHub login."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_logins import is_login


class LoginTests(unittest.TestCase):
    def test_letters_digits_and_single_inner_hyphens_are_a_login(self) -> None:
        for login in ("a", "octocat", "Octo-Cat", "a-b-c", "x1-2y", "a" * 39):
            with self.subTest(login=login):
                self.assertTrue(is_login(login))

    def test_an_enterprise_managed_login_carries_its_short_code_after_one_underscore(self) -> None:
        for login in ("mona_octo", "mona-lisa_octo", "m_o", "a" * 34 + "_octo"):
            with self.subTest(login=login):
                self.assertTrue(is_login(login))

    def test_a_login_longer_than_thirty_nine_characters_is_refused(self) -> None:
        self.assertFalse(is_login("a" * 40))
        self.assertFalse(is_login("a" * 35 + "_octo"))

    def test_a_misplaced_hyphen_or_underscore_is_refused(self) -> None:
        for login in ("-octocat", "octocat-", "octo--cat", "_octocat", "octocat_", "mona_octo_x", "mona_-octo"):
            with self.subTest(login=login):
                self.assertFalse(is_login(login))

    def test_a_character_a_query_or_a_command_line_reads_apart_is_refused(self) -> None:
        for login in ("", "octo cat", "octo:cat", "octo'cat", 'octo"cat', "octo/cat", "octocat\n", "octo.cat", "ö"):
            with self.subTest(login=login):
                self.assertFalse(is_login(login))

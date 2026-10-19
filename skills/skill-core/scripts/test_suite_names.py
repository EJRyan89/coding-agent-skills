"""Regression tests for the repository's rule for a regression suite."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite_names import SUITE_EXTENSIONS, SUITE_NAME_PATTERNS, is_suite_name


class SuiteNameTests(unittest.TestCase):
    def test_the_rule_is_the_five_name_patterns_and_the_three_extensions(self) -> None:
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), SUITE_NAME_PATTERNS)
        self.assertEqual({".py", ".sh", ".ps1"}, SUITE_EXTENSIONS)


@pytest.mark.parametrize(
    "name", ["test_a.py", "test-a.sh", "a_test.ps1", "a-test.py", "a.test.ps1", "Test_A.PY", "A.TEST.sh"]
)
def test_a_suite_is_named_by_its_stem_or_its_whole_name_ignoring_case(name: str) -> None:
    assert is_suite_name(name)


@pytest.mark.parametrize("name", ["test_a.txt", "test_a", "testa.py", "a_tests.py", "contest_a.py", ".py", "test_.md"])
def test_another_extension_or_name_is_not_a_suite(name: str) -> None:
    assert not is_suite_name(name)

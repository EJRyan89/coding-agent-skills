"""Regression tests for the repository's rule for a regression suite."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite_names import SUITE_EXTENSIONS, SUITE_NAME_PATTERNS, is_suite_name


class SuiteNameTests(unittest.TestCase):
    def test_the_rule_is_the_five_name_patterns_and_the_three_extensions(self) -> None:
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), SUITE_NAME_PATTERNS)
        self.assertEqual({".py", ".sh", ".ps1"}, SUITE_EXTENSIONS)

    def test_a_suite_is_named_by_its_stem_or_its_whole_name_ignoring_case(self) -> None:
        for name in ("test_a.py", "test-a.sh", "a_test.ps1", "a-test.py", "a.test.ps1", "Test_A.PY", "A.TEST.sh"):
            with self.subTest(name=name):
                self.assertTrue(is_suite_name(name))

    def test_another_extension_or_name_is_not_a_suite(self) -> None:
        for name in ("test_a.txt", "test_a", "testa.py", "a_tests.py", "contest_a.py", ".py", "test_.md"):
            with self.subTest(name=name):
                self.assertFalse(is_suite_name(name))

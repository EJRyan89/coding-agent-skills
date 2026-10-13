from __future__ import annotations

import unittest

import conditions


class SuiteRuleTests(unittest.TestCase):
    def test_a_suite_is_named_as_validation_names_one_ignoring_case(self) -> None:
        for path in (
            "tests/test_a.py",
            "tests/deployer/test-b.sh",
            "skills/x/scripts/c_test.ps1",
            "skills/x/scripts/d-test.py",
            "skills/x/scripts/e.test.sh",
            "tests/TEST_F.PY",
        ):
            with self.subTest(path=path):
                self.assertTrue(conditions.is_suite(path))
        for path in ("tests/harness.py", "tests/test_a.txt", "tests/contest.py", "tests/a.tests.py", "test_a.js"):
            with self.subTest(path=path):
                self.assertFalse(conditions.is_suite(path))


if __name__ == "__main__":
    unittest.main()

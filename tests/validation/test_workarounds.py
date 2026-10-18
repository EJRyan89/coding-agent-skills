"""Fixture tests for tests/validation/workarounds.py.

Each policy fails on a violating input and passes on a conforming one. The fixtures are text in string literals, so
the policies, which read comments and names, find nothing in this file itself.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from validation_support import write_fixture_tree
from workarounds import later_comment_problems, skip_problems

SUITE = "tests/test_fixture.py"


class WorkaroundFixtures(unittest.TestCase):
    def problems(self, files: dict[str, str]) -> tuple[list[str], list[str]]:
        """The skip and the comment problems of a fixture tree."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            write_fixture_tree(root, files)
            paths = [root / name for name in files]
            return skip_problems(root, paths), later_comment_problems(root, paths)

    def skips(self, source: str) -> list[str]:
        return self.problems({SUITE: source})[0]

    def test_each_skip_and_expected_failure_fails_through_the_module_imports(self) -> None:
        for source, marker in (
            (
                "import unittest\nclass C:\n    @unittest.skip('later')\n    def test_a(self): ...\n",
                "unittest.skip",
            ),
            (
                "import unittest as u\nclass C(u.TestCase):\n    @u.expectedFailure\n    def test_a(self): ...\n",
                "unittest.expectedFailure",
            ),
            (
                "from unittest import skipIf\nclass C:\n    @skipIf(True, 'slow')\n    def test_a(self): ...\n",
                "unittest.skipIf",
            ),
            (
                "import unittest\nclass C:\n    @unittest.skipUnless(FAST, 'slow')\n    def test_a(self): ...\n",
                "unittest.skipUnless",
            ),
            ("class C:\n    def test_a(self):\n        self.skipTest('flaky')\n", "skipTest"),
            (
                "import unittest\nclass C:\n    def test_a(self):\n        raise unittest.SkipTest('flaky')\n",
                "unittest.SkipTest",
            ),
            (
                "from unittest.case import skip\nclass C:\n    @skip('later')\n    def test_a(self): ...\n",
                "unittest.case.skip",
            ),
            ("import pytest\n@pytest.mark.xfail\ndef test_a(): ...\n", "pytest.mark.xfail"),
        ):
            with self.subTest(marker=marker):
                problems = self.skips(source)
                self.assertEqual(1, len(problems), problems)
                self.assertIn(f"with {marker}, so its suite passes without it", problems[0])
                self.assertTrue(problems[0].startswith(f"{SUITE}:"), problems[0])

    def test_a_skip_on_a_platform_alone_passes_and_any_other_condition_fails(self) -> None:
        for condition in (
            "sys.platform == 'win32'",
            "sys.platform != 'win32'",
            "os.name == 'nt' and sys.platform in ('linux', 'darwin')",
            "not sys.platform.startswith('linux')",
            "platform.system() == 'Windows'",
        ):
            with self.subTest(condition=condition):
                source = (
                    "import os, platform, sys, unittest\nclass C(unittest.TestCase):\n"
                    f"    @unittest.skipIf({condition}, 'not on this platform')\n    def test_a(self): ...\n"
                )
                self.assertEqual([], self.skips(source))
                pytest_source = (
                    "import os, platform, sys\nimport pytest\n"
                    f"@pytest.mark.skipif({condition}, reason='not on this platform')\ndef test_a(): ...\n"
                )
                self.assertEqual([], self.skips(pytest_source))
        for condition in ("sys.platform == 'win32' or SLOW", "sys.platform == PLATFORM", "os.environ.get('CI')"):
            with self.subTest(condition=condition):
                source = (
                    "import os, sys, unittest\nclass C(unittest.TestCase):\n"
                    f"    @unittest.skipUnless({condition}, 'not here')\n    def test_a(self): ...\n"
                )
                self.assertEqual(1, len(self.skips(source)))
                pytest_source = (
                    "import os, sys\nfrom pytest import mark\n"
                    f"@mark.skipif({condition}, reason='not here')\ndef test_a(): ...\n"
                )
                self.assertEqual(1, len(self.skips(pytest_source)))

    def test_a_test_a_class_body_defines_on_one_platform_passes(self) -> None:
        source = (
            "import sys, unittest\nclass C(unittest.TestCase):\n"
            "    if sys.platform == 'win32':\n        def test_a(self): ...\n"
        )
        self.assertEqual(([], []), self.problems({SUITE: source}))

    def test_an_allowance_sanctions_its_skip_and_fails_when_it_sanctions_nothing(self) -> None:
        skipped = "import unittest\nclass C(unittest.TestCase):\n    @unittest.skip('x')\n    def test_a(self): ...\n"
        allowed = "SKIP_ALLOWED = {'C.test_a': 'the service it calls is gone'}\n"
        self.assertEqual([], self.skips(allowed + skipped))
        stale = self.skips("SKIP_ALLOWED = {'C.test_b': 'no longer skipped'}\n")
        self.assertEqual(
            [f"{SUITE} SKIP_ALLOWED names C.test_b, which holds no skip or expected failure; remove the entry"], stale
        )
        unreadable = self.skips("SKIP_ALLOWED = {'C.test_a': ''}\n" + skipped)
        self.assertIn(f"{SUITE} SKIP_ALLOWED must map each function or class to its reason", unreadable)
        self.assertEqual(2, len(unreadable))

    def test_a_module_level_skip_is_named_by_the_module(self) -> None:
        problems = self.skips("import pytest\npytest.importorskip('numpy')\n")
        self.assertEqual(1, len(problems))
        self.assertIn("marks <module> with pytest.importorskip", problems[0])

    def test_a_comment_that_leaves_work_for_later_fails_and_text_in_a_string_passes(self) -> None:
        tag = "TO" + "DO"
        source = (
            f"x = 1  # {tag}: finish\n"
            "# FIXME the parser\n"
            "# XXX\n"
            f"TEXT = '# {tag} in a string is no comment'\n"
            "# a template such as tmpXXXXXX, a todo in lower case, and FIXMES are not markers\n"
        )
        comments = self.problems({SUITE: source})[1]
        self.assertEqual(
            [f"{SUITE}:1 leaves {tag}", f"{SUITE}:2 leaves FIXME", f"{SUITE}:3 leaves XXX"],
            [problem.split(" in a comment")[0] for problem in comments],
        )

    def test_files_outside_the_checked_python_pass(self) -> None:
        later = "# FIXME the parser\n"
        skipped = "import unittest\n@unittest.skip('x')\nclass C: ...\n"
        self.assertEqual(
            ([], []),
            self.problems(
                {
                    "tests/fixtures/repo/test_x.py": later + skipped,
                    "docs/notes.py": later + skipped,
                    "skills/a/scripts/run.sh": later,
                }
            ),
        )
        self.assertEqual(1, len(self.problems({"skills/a/scripts/test_x.py": later})[1]))
        self.assertEqual(1, len(self.problems({"deploy.py": later})[1]))

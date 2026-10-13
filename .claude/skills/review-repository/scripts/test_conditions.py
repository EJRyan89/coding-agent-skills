from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

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


class ReadFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "snapshot"
        self.root.mkdir()

    def test_headers_are_read_only_before_a_file_s_first_hunk(self) -> None:
        diff = (
            'diff --git "a/tools/r\\303\\251sum\\303\\251.py" "b/tools/r\\303\\251sum\\303\\251.py"\n'
            'index 1111111..2222222 100644\n--- "a/tools/r\\303\\251sum\\303\\251.py"\n'
            '+++ "b/tools/r\\303\\251sum\\303\\251.py"\n'
            '@@ -1,2 +1,3 @@\n x = 1\n--- "gone\n+++ "oops\n+++ b/tools/elsewhere.py\n'
        )
        files, refused = conditions.read_files(diff, self.root)
        self.assertEqual([], refused)
        self.assertEqual(
            [("tools/résumé.py", "tools/résumé.py", self.root.resolve() / "tools" / "résumé.py")],
            [(file.old, file.new, file.head) for file in files],
        )
        self.assertEqual(
            ("@@ -1,2 +1,3 @@", " x = 1", '--- "gone', '+++ "oops', "+++ b/tools/elsewhere.py", ""), files[0].lines
        )

    def test_a_rename_names_its_old_path_and_its_new_and_an_added_or_deleted_file_one_side(self) -> None:
        diff = (
            "diff --git a/a/old.py b/a/new.py\nsimilarity index 100%\nrename from a/old.py\nrename to a/new.py\n"
            "diff --git a/b.py b/b.py\nnew file mode 100644\n--- /dev/null\n+++ b/b.py\n@@ -0,0 +1 @@\n+x\n"
            "diff --git a/c.py b/c.py\ndeleted file mode 100644\n--- a/c.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
            "diff --git a/d.py b/d.py\nold mode 100644\nnew mode 100755\n"
        )
        files, refused = conditions.read_files(diff, self.root)
        self.assertEqual([], refused)
        self.assertEqual(
            [("a/old.py", "a/new.py", "a/new.py"), (None, "b.py", "b.py"), ("c.py", None, "c.py")],
            [(file.old, file.new, file.path) for file in files],
        )
        self.assertIsNone(files[2].head)

    def test_a_file_any_of_whose_paths_could_land_outside_the_snapshot_is_refused_once(self) -> None:
        sections = [
            'diff --git a/x b/x\n--- a/x\n+++ "b/tools\\\\..\\\\..\\\\outside.py"\n@@ -1 +1 @@\n-a\n+b\n',
            'diff --git a/x b/x\n--- a/x\n+++ "b/tools\\\\..\\\\..\\\\outside.py"\n@@ -1 +1 @@\n-a\n+b\n',
            "diff --git a/x b/x\nrename from ../outside.py\nrename to tools/inside.py\n",
            "diff --git a/x b/x\n--- a/C:/outside.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n",
            "diff --git a/x b/x\n--- /dev/null\n+++ b//outside.py\n@@ -0,0 +1 @@\n+a\n",
            "diff --git a/x b/x\n--- a/kept.py\n+++ b/kept.py\n@@ -1 +1 @@\n-a\n+b\n",
        ]
        files, refused = conditions.read_files("".join(sections), self.root)
        self.assertEqual(["tools\\..\\..\\outside.py", "../outside.py", "C:/outside.py", "/outside.py"], refused)
        self.assertEqual(["kept.py"], [file.path for file in files])
        self.assertEqual(
            'REFUSED "tools\\\\..\\\\..\\\\outside.py": a path that could name a file outside the snapshot',
            conditions.refusal_line(refused[0]),
        )


if __name__ == "__main__":
    unittest.main()

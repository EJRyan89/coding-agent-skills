from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import conditions


class FailureTests(unittest.TestCase):
    def test_a_failure_prints_its_failed_line_and_exits_2_since_1_closes_the_route(self) -> None:
        def broken() -> int:
            raise LookupError("no diff")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(2, conditions.run_condition(broken))
            self.assertEqual(1, conditions.run_condition(lambda: 1))
        self.assertEqual("FAILED LookupError: no diff\n", output.getvalue())

    def test_the_unset_variables_line_names_each_variable_a_review_did_not_set(self) -> None:
        both = {"CODE_REVIEW_DIFF_FILE": "diff.txt", "CODE_REVIEW_CONDITION_OUTPUT": "out.txt"}
        with mock.patch.dict(os.environ, both, clear=True):
            self.assertIsNone(conditions.unset_variables_line())
        with mock.patch.dict(os.environ, {"CODE_REVIEW_DIFF_FILE": "diff.txt"}, clear=True):
            self.assertEqual(
                "FAILED CODE_REVIEW_CONDITION_OUTPUT not set; a review sets them for a condition",
                conditions.unset_variables_line(),
            )
        with mock.patch.dict(os.environ, {"CODE_REVIEW_DIFF_FILE": ""}, clear=True):
            self.assertEqual(
                "FAILED CODE_REVIEW_DIFF_FILE and CODE_REVIEW_CONDITION_OUTPUT not set; a review sets them for a "
                "condition",
                conditions.unset_variables_line(),
            )


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


class SnapshotExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "source-snapshot.json"

    def test_the_exclusions_are_the_ones_the_snapshot_s_manifest_records(self) -> None:
        self.manifest.write_bytes(
            b'{"excluded_paths": {".claude/x.py": "agent-instruction", "big.py": "file-size-limit"}}'
        )
        self.assertEqual(
            {".claude/x.py": "agent-instruction", "big.py": "file-size-limit"},
            conditions.snapshot_exclusions(self.root),
        )

    def test_a_snapshot_with_no_manifest_records_no_exclusion(self) -> None:
        self.assertEqual({}, conditions.snapshot_exclusions(self.root))

    def test_a_manifest_it_cannot_read_raises_missing_evidence_rather_than_excluding_nothing(self) -> None:
        unreadable = "the snapshot's source-snapshot.json cannot be read: "
        unmapped = "the snapshot's source-snapshot.json records no excluded_paths mapping paths to reasons"
        for content, message in (
            (b"not json", unreadable),
            (b"\xff", unreadable),
            (b"[]", unmapped),
            (b"{}", unmapped),
            (b'{"excluded_paths": []}', unmapped),
            (b'{"excluded_paths": {"a": 1}}', unmapped),
        ):
            with self.subTest(content=content):
                self.manifest.write_bytes(content)
                with self.assertRaises(conditions.MissingEvidence) as raised:
                    conditions.snapshot_exclusions(self.root)
                self.assertTrue(str(raised.exception).startswith(message), str(raised.exception))
                self.assertEqual(message == unmapped, str(raised.exception) == unmapped)
        self.manifest.unlink()
        self.manifest.mkdir()
        with self.assertRaises(conditions.MissingEvidence) as raised:
            conditions.snapshot_exclusions(self.root)
        self.assertTrue(str(raised.exception).startswith(unreadable), str(raised.exception))


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "tests/deployer/test-b.sh",
        "skills/x/scripts/c_test.ps1",
        "skills/x/scripts/d-test.py",
        "skills/x/scripts/e.test.sh",
        "tests/TEST_F.PY",
    ],
)
def test_a_suite_is_named_as_validation_names_one_ignoring_case(path: str) -> None:
    assert conditions.is_suite(path)


@pytest.mark.parametrize(
    "path", ["tests/harness.py", "tests/test_a.txt", "tests/contest.py", "tests/a.tests.py", "test_a.js"]
)
def test_a_file_named_otherwise_is_not_a_suite(path: str) -> None:
    assert not conditions.is_suite(path)

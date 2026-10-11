"""Integer versions fail closed at persisted and command-line review boundaries."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_config
import review_pipeline
from console import use_utf8_output
from review_flags import FlagError, add_flag, load_store, upgrade_store
from review_io import atomic_write_json
from review_records import RecordError, validate_record
from review_state import StateError, empty_state, load_state, update_state, validate_state
from test_review_canary import PULL, write_fixture
from test_review_record_validation import _record


class VersionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_state_versions_are_integers_on_reads_and_writes(self) -> None:
        path = self.root / "state.json"
        original = empty_state()
        atomic_write_json(path, original)
        original_bytes = path.read_bytes()
        self.assertEqual(original, validate_state(original))
        self.assertIs(int, type(load_state(path)["schema_version"]))
        for version in (True, 1.0, False, "1", None, 2):
            malformed = {**original, "schema_version": version}
            message = f"Unsupported state schema version: {version!r}"
            with self.subTest(version=version):
                with self.assertRaises(StateError) as caught:
                    validate_state(malformed)
                self.assertEqual(message, str(caught.exception))
                with self.assertRaises(StateError) as caught:
                    update_state(path, mock.Mock(return_value=malformed))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual(original_bytes, path.read_bytes())
                path.write_text(json.dumps(malformed), encoding="utf-8")
                malformed_bytes = path.read_bytes()
                with self.assertRaises(StateError) as caught:
                    load_state(path)
                self.assertEqual(message, str(caught.exception))
                self.assertEqual(malformed_bytes, path.read_bytes())
                path.write_bytes(original_bytes)

    def test_flag_migration_recognizes_only_the_integer_legacy_version(self) -> None:
        path = self.root / "flags.json"
        for version in (True, 1.0, 2.0):
            malformed: dict[str, Any] = {"schema_version": version, "next_id": 1, "flags": []}
            with self.subTest(version=version):
                self.assertEqual(malformed, upgrade_store(malformed))
                path.write_text(json.dumps(malformed), encoding="utf-8")
                before = path.read_bytes()
                for action in (lambda: load_store(path), lambda: add_flag(path, category="Noise", body="Reason.")):
                    with self.assertRaises(FlagError) as caught:
                        action()
                    self.assertEqual("Flag store schema version is unsupported", str(caught.exception))
                    self.assertEqual(before, path.read_bytes())
        path.write_text(json.dumps({"schema_version": 1, "next_id": 1, "flags": []}), encoding="utf-8")
        self.assertEqual(2, load_store(path)["schema_version"])

    def test_prepared_run_version_is_checked_before_any_run_field_is_read(self) -> None:
        path = self.root / review_pipeline.RUN_FILE
        for version in (True, 1.0):
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            with self.subTest(version=version), self.assertRaises(review_pipeline.PipelineError) as caught:
                review_pipeline.load_run(self.root)
            self.assertEqual(f"{self.root} is not a prepared review run", str(caught.exception))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = review_pipeline.main(["check", "--run", str(self.root)])
            self.assertEqual(1, result)
            self.assertEqual(f"FAILED {self.root} {self.root} is not a prepared review run\n", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
        value = {"schema_version": review_pipeline.RUN_SCHEMA_VERSION}
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(value, review_pipeline.load_run(self.root))

    def test_batch_version_is_checked_before_configuration_or_watermarks(self) -> None:
        path = self.root / "batch.json"
        for version in (True, 1.0):
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            before = path.read_bytes()
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(review_pipeline, "load_config") as load_config,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = review_pipeline.main(["advance", "--batch", str(path)])
            with self.subTest(version=version):
                self.assertEqual(1, result)
                self.assertEqual(f"FAILED {path} is not a review batch file\n", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
                self.assertEqual(before, path.read_bytes())
                load_config.assert_not_called()
        batch = {"schema_version": review_pipeline.BATCH_SCHEMA_VERSION, "today": "2026-10-11", "repositories": {}}
        path.write_text(json.dumps(batch), encoding="utf-8")
        with mock.patch.object(review_pipeline, "load_config", return_value={"archive_root": str(self.root)}):
            self.assertEqual({}, review_pipeline.advance_watermarks(path))

    def test_configuration_cli_reports_invalid_versions_without_writing(self) -> None:
        path = self.root / "config.json"
        for version in (True, 1.0):
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            before = path.read_bytes()
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = review_config._main(["validate", str(path)])
            with self.subTest(version=version):
                self.assertEqual(1, result)
                self.assertEqual(f"FAILED Unsupported config schema version: {version!r}\n", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
                self.assertEqual(before, path.read_bytes())

    def test_patch_paths_share_the_repository_relative_path_contract(self) -> None:
        record = _record()
        patch = {"sha256": "a" * 64, "lines": 1}
        for path in ("C:/x.py", "c:/x.py", "C:x.py", "C:\\x.py"):
            record["review"]["patches"] = {path: patch}
            with self.subTest(path=path), self.assertRaises(RecordError) as caught:
                validate_record(record)
            self.assertEqual("Review patch path is unsafe", str(caught.exception))
        for path in ("src/x.py", "src/space name.py", "src/a:b.py", ".github/workflows/check.yml"):
            record["review"]["patches"] = {path: patch}
            self.assertEqual(record, validate_record(record))

    def test_fixture_cli_refuses_non_integer_versions_before_git_or_run_writes(self) -> None:
        fixture = write_fixture(self.root / "fixture", {"a.py": b"before\n"}, {"a.py": b"after\n"})
        pull_path = fixture / "pull.json"
        for version in (True, 1.0):
            pull_path.write_text(json.dumps({**PULL, "schema_version": version}), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            git = mock.Mock(side_effect=AssertionError("An invalid fixture must not run git"))
            with (
                mock.patch.object(review_pipeline, "load_config", return_value={}),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = review_pipeline.main(
                    ["prepare", "--canary", "--fixture", str(fixture)],
                    services=review_pipeline.Services(git=git),
                )
            with self.subTest(version=version):
                self.assertEqual(1, result)
                self.assertEqual(f"FAILED {fixture} pull.json schema_version must be 1\n", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
                git.assert_not_called()
                self.assertEqual(["fixture"], [path.name for path in self.root.iterdir()])

    def test_repository_paths_keep_posix_names_without_accepting_drives(self) -> None:
        for path in (
            "src/x.py",
            "./src//x.py",
            "src/a:b.py",
            "1:notes.md",
            "notes:archive/x.md",
            "docs/space name.md",
            "suite:design-review",
        ):
            with self.subTest(path=path):
                self.assertTrue(review_config.safe_repository_path(path))
        for path in ("C:/x.py", "c:/x.py", "C:x.py", "C:\\x.py", "/x.py", "../x.py", "src\\x.py"):
            with self.subTest(path=path):
                self.assertFalse(review_config.safe_repository_path(path))


if __name__ == "__main__":
    use_utf8_output()
    unittest.main()

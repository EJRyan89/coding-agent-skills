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
from git_test_base import GitTestCase
from review_flags import FlagError, FlagFieldError, add_flag, load_store, upgrade_store
from review_io import atomic_write_json
from review_records import RecordError, validate_record
from review_state import StateError, StateFieldError, empty_state, load_state, update_state, validate_state
from test_review_canary import PULL, write_fixture
from test_review_record_validation import _record
from typed_fields import FieldError


class VersionBoundaryTests(GitTestCase):
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
        kind = "state.schema_version must be an integer, found"
        cases: list[tuple[Any, type[StateError], str]] = [
            (True, StateFieldError, f"{kind} boolean true"),
            (1.0, StateFieldError, f"{kind} number 1.0"),
            (False, StateFieldError, f"{kind} boolean false"),
            ("1", StateFieldError, f'{kind} string "1"'),
            (None, StateFieldError, f"{kind} null"),
            (2, StateError, "Unsupported state schema version: 2"),
        ]
        for version, error, message in cases:
            malformed = {**original, "schema_version": version}
            with self.subTest(version=version):
                with self.assertRaises(StateError) as caught:
                    validate_state(malformed)
                self.assertEqual((error, message), (type(caught.exception), str(caught.exception)))
                with self.assertRaises(StateError) as caught:
                    update_state(path, mock.Mock(return_value=malformed))
                self.assertEqual((error, message), (type(caught.exception), str(caught.exception)))
                self.assertEqual(original_bytes, path.read_bytes())
                path.write_text(json.dumps(malformed), encoding="utf-8")
                malformed_bytes = path.read_bytes()
                with self.assertRaises(StateError) as caught:
                    load_state(path)
                self.assertEqual((error, message), (type(caught.exception), str(caught.exception)))
                self.assertEqual(malformed_bytes, path.read_bytes())
                path.write_bytes(original_bytes)

    def test_state_fields_of_the_wrong_kind_are_refused_in_one_shape(self) -> None:
        self.assertTrue(issubclass(StateFieldError, FieldError) and issubclass(StateFieldError, StateError))
        entry = "state.repositories.owner/repo"

        def repository(value: Any) -> dict[str, Any]:
            return {"schema_version": 1, "repositories": {"owner/repo": value}}

        cases: list[tuple[str, Any, str]] = [
            ("a state that is an array", [], "state must be an object, found an array"),
            ("a state that is a string", "state", 'state must be an object, found string "state"'),
            (
                "an unknown state field",
                {"schema_version": 1, "repositories": {}, "extra": 1},
                'state must be an object of known fields, found unknown field "extra"',
            ),
            ("no schema version", {"repositories": {}}, "state.schema_version must be an integer, found null"),
            (
                "repositories that are an array",
                {"schema_version": 1, "repositories": []},
                "state.repositories must be an object, found an array",
            ),
            ("no repositories", {"schema_version": 1}, "state.repositories must be an object, found null"),
            ("an entry that is a string", repository("x"), f'{entry} must be an object, found string "x"'),
            (
                "an unknown entry field",
                repository({"extra": 1}),
                f'{entry} must be an object of known fields, found unknown field "extra"',
            ),
            (
                "a merged_since that is an integer",
                repository({"merged_since": 5}),
                f"{entry}.merged_since must be a string, found integer 5",
            ),
            (
                "a merged_since that is an array",
                repository({"merged_since": ["2026-10-11"]}),
                f"{entry}.merged_since must be a string, found an array",
            ),
            (
                "an updated_at that is null",
                repository({"updated_at": None}),
                f"{entry}.updated_at must be a string, found null",
            ),
        ]
        for name, value, message in cases:
            with self.subTest(name):
                with self.assertRaises(StateError) as caught:
                    validate_state(value)
                self.assertEqual((StateFieldError, message), (type(caught.exception), str(caught.exception)))

    def test_flag_migration_recognizes_only_the_integer_legacy_version(self) -> None:
        path = self.root / "flags.json"
        kind = "flag-store.schema_version must be an integer, found"
        for version, found in ((True, "boolean true"), (1.0, "number 1.0"), (2.0, "number 2.0")):
            malformed: dict[str, Any] = {"schema_version": version, "next_id": 1, "flags": []}
            with self.subTest(version=version):
                self.assertEqual(malformed, upgrade_store(malformed))
                path.write_text(json.dumps(malformed), encoding="utf-8")
                before = path.read_bytes()
                for action in (lambda: load_store(path), lambda: add_flag(path, category="Noise", body="Reason.")):
                    with self.assertRaises(FlagError) as caught:
                        action()
                    self.assertIs(FlagFieldError, type(caught.exception))
                    self.assertEqual(f"{kind} {found}", str(caught.exception))
                    self.assertEqual(before, path.read_bytes())
        path.write_text(json.dumps({"schema_version": 1, "next_id": 1, "flags": []}), encoding="utf-8")
        self.assertEqual(2, load_store(path)["schema_version"])

    def test_prepared_run_version_is_checked_before_any_run_field_is_read(self) -> None:
        path = self.root / review_pipeline.RUN_FILE
        for version, found in ((True, "boolean true"), (1.0, "number 1.0")):
            message = f"run.schema_version must be an integer, found {found}"
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            with self.subTest(version=version):
                with self.assertRaises(review_pipeline.PipelineError) as caught:
                    review_pipeline.load_run(self.root)
                self.assertIs(review_pipeline.PipelineFieldError, type(caught.exception))
                self.assertEqual(message, str(caught.exception))
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = review_pipeline.main(["check", "--run", str(self.root)])
                self.assertEqual(1, result)
                self.assertEqual(f"FAILED {self.root} {message}\n", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
        value = {"schema_version": review_pipeline.RUN_SCHEMA_VERSION}
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(value, review_pipeline.load_run(self.root))

    def test_batch_version_is_checked_before_configuration_or_watermarks(self) -> None:
        path = self.root / "batch.json"
        for version, found in ((True, "boolean true"), (1.0, "number 1.0")):
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
                self.assertEqual(f"FAILED batch.schema_version must be an integer, found {found}\n", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
                self.assertEqual(before, path.read_bytes())
                load_config.assert_not_called()
        batch = {"schema_version": review_pipeline.BATCH_SCHEMA_VERSION, "today": "2026-10-11", "repositories": {}}
        path.write_text(json.dumps(batch), encoding="utf-8")
        with mock.patch.object(review_pipeline, "load_config", return_value={"archive_root": str(self.root)}):
            self.assertEqual({}, review_pipeline.advance_watermarks(path))

    def test_configuration_cli_reports_invalid_versions_without_writing(self) -> None:
        path = self.root / "config.json"
        for version, found in ((True, "boolean true"), (1.0, "number 1.0")):
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            before = path.read_bytes()
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = review_config._main(["validate", str(path)])
            with self.subTest(version=version):
                self.assertEqual(1, result)
                self.assertEqual(f"FAILED config.schema_version must be an integer, found {found}\n", stdout.getvalue())
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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for version, found in ((True, "boolean true"), (1.0, "number 1.0")):
            pull_path.write_text(json.dumps({**PULL, "schema_version": version}), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            git = mock.Mock(side_effect=AssertionError("An invalid fixture must not run git"))
            with (
                mock.patch.object(review_pipeline, "load_config", return_value={}),
                mock.patch.object(tempfile, "tempdir", temporary.name),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = review_pipeline.main(
                    ["prepare", "--canary", "--fixture", str(fixture)],
                    services=review_pipeline.Services(git=git),
                )
            with self.subTest(version=version):
                self.assertEqual(1, result)
                failed, listed = stdout.getvalue().splitlines(keepends=True)
                self.assertEqual(
                    f"FAILED {fixture} fixture-pull.schema_version must be an integer, found {found}\n", failed
                )
                # The run list prepare closes with records no run, and nothing left to prepare.
                runs = review_pipeline.load_run_list(Path(listed.removeprefix("RUNS ").removesuffix(" pending=0\n")))
                self.assertEqual(([], []), (runs["runs"], runs["pending"]))
                self.assertEqual("", stderr.getvalue())
                git.assert_not_called()
                self.assertEqual(["fixture"], [path.name for path in self.root.iterdir()])
                self.assertEqual(
                    [],
                    [
                        path
                        for path in Path(temporary.name).iterdir()
                        if not path.name.startswith(review_pipeline.RUN_LIST_PREFIX)
                    ],
                )

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

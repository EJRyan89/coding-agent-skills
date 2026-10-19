"""The flag store: adding, editing, resolving, and validating improvement flags."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from review_flags import FlagAlreadyResolved, FlagError, add_flag, edit_flag, load_store, resolve_flag


class FlagTests(unittest.TestCase):
    def test_add_and_resolve_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            created = add_flag(
                path,
                category="Guideline",
                body="Consider a repository rule.",
                repository="example/one",
                pull_number=12,
                review_version=2,
                finding_id="F001",
            )
            self.assertEqual("RF-000001", created["id"])
            self.assertEqual(2, created["review_version"])
            resolved = resolve_flag(path, created["id"], "Accepted")
            self.assertEqual("resolved", resolved["status"])
            self.assertEqual(2, load_store(path)["next_id"])
            self.assertEqual(2, json.loads(path.read_text(encoding="utf-8"))["schema_version"])
            before = path.read_bytes()
            with self.assertRaises(FlagAlreadyResolved) as caught:
                resolve_flag(path, created["id"], "Again")
            self.assertIsInstance(caught.exception, FlagError)
            self.assertEqual(created["id"], caught.exception.flag_id)
            self.assertEqual(f"Flag is already resolved: {created['id']}", str(caught.exception))
            self.assertEqual(before, path.read_bytes())

    def test_resolution_validation_and_unknown_ids_are_not_already_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            created = add_flag(path, category="guideline", body="Body")
            resolve_flag(path, created["id"], "First")
            before = path.read_bytes()
            for flag_id, resolution, message in (
                (created["id"], " \t", "Resolution is required"),
                ("RF-000009", "Done", "Unknown flag ID: RF-000009"),
            ):
                with self.subTest(flag_id=flag_id), self.assertRaises(FlagError) as caught:
                    resolve_flag(path, flag_id, resolution)
                self.assertIs(FlagError, type(caught.exception))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual(before, path.read_bytes())

    def test_edit_changes_only_the_category_and_body_of_an_open_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            created = add_flag(
                path,
                category="Guideline",
                body="First rationale.",
                repository="example/one",
                pull_number=12,
                review_version=2,
                finding_id="F001",
            )
            edited = edit_flag(path, created["id"], category="noise", body="Corrected rationale.")
            expected = {**created, "category": "noise", "body": "Corrected rationale."}
            self.assertEqual(expected, edited)
            self.assertEqual([expected], load_store(path)["flags"])
            self.assertEqual(2, load_store(path)["next_id"])
            body_only = edit_flag(path, created["id"], body="Body alone.")
            self.assertEqual({**expected, "body": "Body alone."}, body_only)
            category_only = edit_flag(path, created["id"], category="missed")
            self.assertEqual({**expected, "body": "Body alone.", "category": "missed"}, category_only)

    def test_edit_refusals_name_their_reason_and_leave_the_store_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            open_flag = add_flag(path, category="guideline", body="Open.")
            resolved = add_flag(path, category="guideline", body="Resolved.")
            resolve_flag(path, resolved["id"], "Done")
            before = path.read_bytes()
            refusals: tuple[tuple[str, dict[str, str], str], ...] = (
                (resolved["id"], {"body": "New."}, f"Flag {resolved['id']} is resolved; only an open flag is edited"),
                ("RF-000009", {"body": "New."}, "Unknown flag ID: RF-000009"),
                (open_flag["id"], {"category": " \t"}, "Flag category must not be blank"),
                (open_flag["id"], {"category": ""}, "Flag category must not be blank"),
                (open_flag["id"], {"body": "\n"}, "Flag body must not be blank"),
                (open_flag["id"], {}, "Name a new category, a new body, or both"),
            )
            for flag_id, changes, message in refusals:
                with self.subTest(flag_id=flag_id, changes=changes), self.assertRaises(FlagError) as caught:
                    edit_flag(path, flag_id, **changes)
                self.assertIs(FlagError, type(caught.exception))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual(before, path.read_bytes())

    def test_edit_of_a_value_of_the_wrong_kind_fails_before_the_store_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            with self.assertRaises(FlagError):
                edit_flag(path, "RF-000001", category=7)  # type: ignore[arg-type]  # a caller outside the CLI
            self.assertFalse(path.exists())

    def test_a_finding_is_named_with_its_review_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            invalid_options: tuple[dict[str, Any], ...] = (
                {"repository": "example/one", "pull_number": 12, "finding_id": "F001"},
                {"repository": "example/one", "review_version": 1, "finding_id": "F001"},
                {"repository": "example/one", "pull_number": 12, "review_version": 0, "finding_id": "F001"},
                {"repository": "example/one", "review_version": 1},
                {"repository": "example/one", "pull_number": 12, "review_version": True},
            )
            for options in invalid_options:
                with self.subTest(options=options), self.assertRaises(FlagError):
                    add_flag(path, category="Guideline", body="Body.", **options)
            self.assertFalse(path.exists())
            unbound = add_flag(path, category="Guideline", body="Body.", repository="example/one", pull_number=12)
            self.assertIsNone(unbound["review_version"])

    def test_version_one_stores_are_upgraded_without_review_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            legacy = {
                "id": "RF-000001",
                "status": "open",
                "created_at": "2026-01-16T00:00:00+00:00",
                "resolved_at": None,
                "repository": "example/one",
                "pull_number": 12,
                "finding_id": "F001",
                "category": "Guideline",
                "body": "Body.",
                "resolution": None,
            }
            path.write_text(json.dumps({"schema_version": 1, "next_id": 2, "flags": [legacy]}), encoding="utf-8")
            self.assertEqual({**legacy, "review_version": None}, load_store(path)["flags"][0])
            add_flag(path, category="Guideline", body="Second.")
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(2, written["schema_version"])
            self.assertEqual([None, None], [flag["review_version"] for flag in written["flags"]])
            path.write_text(json.dumps({"schema_version": 3, "next_id": 1, "flags": []}), encoding="utf-8")
            with self.assertRaisesRegex(FlagError, "schema version is unsupported"):
                load_store(path)

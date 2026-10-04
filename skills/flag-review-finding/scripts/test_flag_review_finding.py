from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("flag_review_finding.py")


class FlagCliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_add_list_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = str(Path(temporary) / "flags.json")
            created = self.run_cli(
                "--store", store, "add", "guideline", "Clarify boundary", "--repository",
                "example/one", "--pull", "12", "--review-version", "2", "--finding", "F001",
            )
            self.assertEqual(0, created.returncode, created.stderr)
            flag_id = json.loads(created.stdout)["id"]
            self.assertEqual(2, json.loads(created.stdout)["review_version"])
            listed = self.run_cli("--store", store, "list")
            self.assertEqual([flag_id], [item["id"] for item in json.loads(listed.stdout)])
            resolved = self.run_cli("--store", store, "resolve", flag_id, "Accepted")
            self.assertEqual("resolved", json.loads(resolved.stdout)["status"])
            self.assertEqual([], json.loads(self.run_cli("--store", store, "list").stdout))

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # Windows pipes default to a legacy code page; a flag body is echoed exactly as the user wrote it.
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--store", str(Path(temporary) / "flags.json"),
                 "add", "guideline", "Prefer → over -> in prose ✓"],
                capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
            self.assertEqual("Prefer → over -> in prose ✓", json.loads(result.stdout.decode("utf-8"))["body"])

    def test_store_defaults_to_the_core_flag_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "flag store" / "flags.json"
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "add", "guideline", "Body"],
                capture_output=True, text=True, encoding="utf-8", check=False,
                env={**os.environ, "CODE_REVIEW_FLAGS": str(store)},
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("RF-000001", json.loads(store.read_text(encoding="utf-8"))["flags"][0]["id"])

    def test_a_finding_without_its_review_version_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "flags.json"
            result = self.run_cli(
                "--store", str(store), "add", "guideline", "Body", "--repository", "example/one",
                "--pull", "12", "--finding", "F001",
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("review version", result.stderr)
            self.assertFalse(store.exists())

    def test_invalid_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_cli(
                "--store", str(Path(temporary) / "flags.json"),
                "add", "guideline", "Body", "--repository", "short-name",
            )
            self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()

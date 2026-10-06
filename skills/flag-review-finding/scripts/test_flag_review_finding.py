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
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.store = str(Path(self._temporary.name) / "flags.json")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def store_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_cli("--store", self.store, *arguments)

    def assert_result(self, result: subprocess.CompletedProcess[str], code: int, stdout: str) -> None:
        self.assertEqual((code, stdout, ""), (result.returncode, result.stdout, result.stderr))

    def test_add_list_and_resolve(self) -> None:
        created = self.store_cli(
            "add", "guideline", "Clarify boundary", "--repository",
            "example/one", "--pull", "12", "--review-version", "2", "--finding", "F001",
        )
        self.assert_result(created, 0, "ADDED RF-000001\n")
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual((2, "F001"), (stored["review_version"], stored["finding_id"]))
        self.assert_result(
            self.store_cli("list"), 0, "FLAG RF-000001 guideline example/one#12 v2 F001 Clarify boundary\nCOUNT 1\n"
        )
        self.assert_result(self.store_cli("resolve", "RF-000001", "Accepted"), 0, "RESOLVED RF-000001\n")
        self.assert_result(self.store_cli("list"), 0, "COUNT 0\n")

    def test_resolving_a_resolved_flag_keeps_its_resolution(self) -> None:
        self.store_cli("add", "guideline", "Body")
        self.store_cli("resolve", "RF-000001", "First")
        self.assert_result(self.store_cli("resolve", "RF-000001", "Second"), 0, "ALREADY_RESOLVED RF-000001\n")
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual("First", stored["resolution"])

    def test_list_prints_one_line_per_open_flag(self) -> None:
        long_body = "word " * 50
        self.store_cli("add", "noise", "No pull request")
        self.store_cli("add", "missed", "Pull only", "--repository", "example/one", "--pull", "7")
        self.store_cli("add", "guideline", "Line one\n\n  line\ttwo", "--repository", "example/one")
        self.store_cli("add", "noise", long_body)
        self.store_cli("add", "noise", "Resolved later")
        self.store_cli("resolve", "RF-000005", "Done")
        self.assert_result(
            self.store_cli("list"),
            0,
            "FLAG RF-000001 noise - No pull request\n"
            "FLAG RF-000002 missed example/one#7 Pull only\n"
            "FLAG RF-000003 guideline example/one Line one line two\n"
            f"FLAG RF-000004 noise - {' '.join(['word'] * 32)}…\n"
            "COUNT 4\n",
        )

    def test_body_of_exactly_160_characters_is_not_cut(self) -> None:
        body = "x" * 160
        self.store_cli("add", "noise", body)
        self.assert_result(self.store_cli("list"), 0, f"FLAG RF-000001 noise - {body}\nCOUNT 1\n")

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # Windows pipes default to a legacy code page; a flag body is echoed exactly as the user wrote it.
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        for arguments, expected in (
            (["add", "guideline", "Prefer → over -> in prose ✓"], "ADDED RF-000001\n"),
            (["list"], "FLAG RF-000001 guideline - Prefer → over -> in prose ✓\nCOUNT 1\n"),
        ):
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--store", self.store, *arguments],
                capture_output=True, env=environment, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(expected, result.stdout.decode("utf-8").replace("\r\n", "\n"))

    def test_store_defaults_to_the_core_flag_path(self) -> None:
        store = Path(self._temporary.name) / "flag store" / "flags.json"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "add", "guideline", "Body"],
            capture_output=True, text=True, encoding="utf-8", check=False,
            env={**os.environ, "CODE_REVIEW_FLAGS": str(store)},
        )
        self.assert_result(result, 0, "ADDED RF-000001\n")
        self.assertEqual("RF-000001", json.loads(store.read_text(encoding="utf-8"))["flags"][0]["id"])

    def test_a_finding_without_its_review_version_fails(self) -> None:
        result = self.store_cli(
            "add", "guideline", "Body", "--repository", "example/one", "--pull", "12", "--finding", "F001",
        )
        self.assert_result(
            result, 1,
            "FAILED A flag that names a finding must name its repository, pull request, and review version\n",
        )
        self.assertFalse(Path(self.store).exists())

    def test_invalid_repository_fails(self) -> None:
        result = self.store_cli("add", "guideline", "Body", "--repository", "short-name")
        self.assert_result(result, 1, "FAILED Invalid repository identity: 'short-name'\n")
        self.assertFalse(Path(self.store).exists())

    def test_unknown_flag_fails(self) -> None:
        self.assert_result(self.store_cli("resolve", "RF-000009", "Done"), 1, "FAILED Unknown flag ID: RF-000009\n")

    def test_unreadable_store_fails(self) -> None:
        Path(self.store).write_text("{not json", encoding="utf-8")
        result = self.store_cli("list")
        self.assertEqual((1, ""), (result.returncode, result.stderr))
        self.assertRegex(result.stdout, r"\AFAILED \S[^\n]*\n\Z")

    def test_usage_errors_exit_2(self) -> None:
        for arguments in (["add", "guideline"], ["add", "guideline", "Body", "--pull", "twelve"], ["remove"]):
            with self.subTest(arguments=arguments):
                result = self.store_cli(*arguments)
                self.assertEqual((2, ""), (result.returncode, result.stdout))
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()

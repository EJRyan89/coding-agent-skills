"""The review_config.py command line: a result line and exit 0, a FAILED line on stdout and exit 1, usage exit 2."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import review_config  # noqa: E402


def valid_config() -> dict:
    return {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {"primary": ["Example/One"]},
        "repositories": {
            "example/one": {
                "reviewer": {"id": "generic", "protocol_version": 1, "trusted_ref": None, "scope": "generic",
                             "manifest_path": None},
                "checkout_path": "C:\\Repos\\One",
            }
        },
        "archive_root": "C:\\Reviews\\Archive",
        "local_mirror_root": "C:\\Reviews\\Mirror",
        "summary_root": "C:\\Reviews\\Summaries",
        "dashboard_file": "C:\\Reviews\\Dashboard.md",
        "github_login": "reviewer",
        "runtime": "auto",
        "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        "dashboard": {},
    }


class ConfigCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "review config"
        self.root.mkdir()

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review_config._main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_validate_names_the_config_it_accepted(self) -> None:
        path = self.write_json("config.json", valid_config())
        self.assertEqual((0, f"VALID {path}\n", ""), self.run_main("validate", str(path)))

    def test_validate_defaults_to_the_configured_path(self) -> None:
        path = self.write_json("config.json", valid_config())
        with mock.patch.dict(os.environ, {"CODE_REVIEW_CONFIG": str(path)}):
            self.assertEqual((0, f"VALID {path}\n", ""), self.run_main("validate"))

    def test_an_invalid_config_is_a_failed_line_on_stdout(self) -> None:
        path = self.write_json("config.json", {**valid_config(), "schema_version": 2})
        self.assertEqual((1, "FAILED Unsupported future config schema version: 2\n", ""),
                         self.run_main("validate", str(path)))

    def test_a_missing_config_is_a_failed_line_on_stdout(self) -> None:
        path = self.root / "absent.json"
        code, out, err = self.run_main("validate", str(path))
        self.assertEqual((1, ""), (code, err))
        self.assertTrue(out.startswith(f"FAILED Cannot read valid JSON from {path}: "), out)
        self.assertEqual(1, len(out.splitlines()), out)

    def test_write_names_the_config_it_wrote(self) -> None:
        candidate = self.write_json("candidate.json", valid_config())
        output = self.root / "config.json"
        self.assertEqual((0, f"WROTE {output}\n", ""), self.run_main("write", str(candidate), "--output", str(output)))
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(["example/one"], written["repository_sets"]["primary"], "it writes the normalized config")

    def test_write_defaults_to_the_configured_path(self) -> None:
        candidate = self.write_json("candidate.json", valid_config())
        output = self.root / "default.json"
        with mock.patch.dict(os.environ, {"CODE_REVIEW_CONFIG": str(output)}):
            self.assertEqual((0, f"WROTE {output}\n", ""), self.run_main("write", str(candidate)))
        self.assertTrue(output.is_file())

    def test_write_refuses_an_invalid_candidate_and_writes_nothing(self) -> None:
        output = self.root / "config.json"
        broken = self.root / "broken.json"
        broken.write_text('{"schema_version": 1,', encoding="utf-8")
        undecodable = self.root / "undecodable.json"
        undecodable.write_bytes(b'{"github_login": "caf\xe9"}')
        cases = (
            (self.write_json("invalid.json", {**valid_config(), "runtime": "nowhere"}),
             "Unknown runtime host: 'nowhere'"),
            (broken, "Expecting property name enclosed in double quotes"),
            (undecodable, "can't decode byte 0xe9"),
            (self.root / "absent.json", "absent.json"),
        )
        for candidate, reason in cases:
            with self.subTest(candidate=candidate.name):
                code, out, err = self.run_main("write", str(candidate), "--output", str(output))
                self.assertEqual((1, ""), (code, err))
                self.assertTrue(out.startswith("FAILED "), out)
                self.assertIn(reason, out)
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertFalse(output.exists())

    def test_arguments_argparse_rejects_are_usage_errors(self) -> None:
        for arguments in ([], ["write"], ["validate", "a.json", "b.json"], ["remove"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(*arguments)
                self.assertEqual(2, raised.exception.code)

    def test_the_script_reports_through_its_exit_code_and_stdout(self) -> None:
        script = str(SCRIPT_DIRECTORY / "review_config.py")
        absent = self.root / "absent.json"

        def run(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-B", script, *arguments], capture_output=True, text=True,
                                  encoding="utf-8", check=False)

        failed = run("validate", str(absent))
        self.assertEqual((1, ""), (failed.returncode, failed.stderr))
        self.assertTrue(failed.stdout.startswith(f"FAILED Cannot read valid JSON from {absent}: "), failed.stdout)
        path = self.write_json("config.json", valid_config())
        valid = run("validate", str(path))
        self.assertEqual((0, f"VALID {path}\n", ""), (valid.returncode, valid.stdout, valid.stderr))
        usage = run("validate", "a.json", "b.json")
        self.assertEqual((2, ""), (usage.returncode, usage.stdout))
        self.assertIn("usage:", usage.stderr)


if __name__ == "__main__":
    unittest.main()

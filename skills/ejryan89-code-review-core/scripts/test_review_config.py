"""The review_config.py command line: a result line and exit 0, a FAILED line on stdout and exit 1, usage exit 2."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_config
from review_config import (
    ConfigurationError,
    ConfigurationFieldError,
    default_manifest_path,
    resolve_repositories,
    validate_config,
    write_config,
)
from review_io import PersistenceError
from test_review_config_validation import ABSOLUTE_PATH, CONTAINER_CASES, _config, native
from test_review_records import field_pattern

SCRIPT_DIRECTORY = Path(__file__).resolve().parent


def valid_config() -> dict:
    return {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {"primary": ["Example/One"]},
        "repositories": {
            "example/one": {
                "reviewer": {
                    "id": "generic",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "generic",
                    "manifest_path": None,
                },
                "checkout_path": native("C:\\Repos\\One"),
            }
        },
        "archive_root": native("C:\\Reviews\\Archive"),
        "local_mirror_root": native("C:\\Reviews\\Mirror"),
        "summary_root": native("C:\\Reviews\\Summaries"),
        "dashboard_file": native("C:\\Reviews\\Dashboard.md"),
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
        self.assertEqual(
            (1, "FAILED Unsupported future config schema version: 2\n", ""), self.run_main("validate", str(path))
        )

    def test_a_missing_config_is_a_failed_line_on_stdout(self) -> None:
        path = self.root / "absent.json"
        code, out, err = self.run_main("validate", str(path))
        self.assertEqual((1, ""), (code, err))
        self.assertTrue(out.startswith(f"FAILED Cannot read valid JSON from {path}: "), out)
        self.assertEqual(1, len(out.splitlines()), out)

    def test_container_fields_fail_validate_and_write_without_changing_files(self) -> None:
        script = str(SCRIPT_DIRECTORY / "review_config.py")
        output = self.write_json("output.json", valid_config())
        before = output.read_bytes()
        for name, mutation, _error, message in CONTAINER_CASES:
            with self.subTest(name=name):
                candidate = self.write_json("candidate.json", mutation(_config()))
                original = candidate.read_bytes()
                for arguments in (
                    ("validate", str(candidate)),
                    ("write", str(candidate), "--output", str(output)),
                ):
                    with self.subTest(command=arguments[0]):
                        result = subprocess.run(
                            [sys.executable, "-B", script, *arguments],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            check=False,
                        )
                        self.assertEqual(
                            (1, f"FAILED {message}\n", ""), (result.returncode, result.stdout, result.stderr)
                        )
                        self.assertEqual(before, output.read_bytes())
                        self.assertEqual(original, candidate.read_bytes())

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
            (
                self.write_json("invalid.json", {**valid_config(), "runtime": "nowhere"}),
                'config.runtime must be one of "auto", "claude-code", "codex", "copilot-cli", found string "nowhere"',
            ),
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
            return subprocess.run(
                [sys.executable, "-B", script, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        failed = run("validate", str(absent))
        self.assertEqual((1, ""), (failed.returncode, failed.stderr))
        self.assertTrue(failed.stdout.startswith(f"FAILED Cannot read valid JSON from {absent}: "), failed.stdout)
        path = self.write_json("config.json", valid_config())
        valid = run("validate", str(path))
        self.assertEqual((0, f"VALID {path}\n", ""), (valid.returncode, valid.stdout, valid.stderr))
        usage = run("validate", "a.json", "b.json")
        self.assertEqual((2, ""), (usage.returncode, usage.stdout))
        self.assertIn("usage:", usage.stderr)


MODEL_NAME_KEY = (
    "a key of config.model_names must be a non-empty trimmed single-line string of at most 200 characters, found"
)


class ConfigurationTests(unittest.TestCase):
    def test_model_names_map_model_identifiers_to_display_names(self) -> None:
        self.assertEqual({}, validate_config(valid_config())["model_names"])
        arn = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc"
        config = validate_config({**valid_config(), "model_names": {arn: "  Opus 5.5 "}})
        self.assertEqual({arn: "Opus 5.5"}, config["model_names"])
        for value, message in (
            ([], "model_names must be an object"),
            ({"": "Opus"}, f"{MODEL_NAME_KEY} an empty string"),
            ({"a\nb": "Opus"}, f"{MODEL_NAME_KEY} a string of more than one line"),
            ({"m" * 201: "Opus"}, f"{MODEL_NAME_KEY} a string of 201 characters"),
            ({arn: ""}, f"model_names.{arn}"),
            ({arn: "Opus\n5"}, f"model_names.{arn}"),
            ({arn: "O" * 101}, f"model_names.{arn}"),
            ({arn: 5}, f"model_names.{arn}"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                validate_config({**valid_config(), "model_names": value})

    def test_config_normalizes_repository_keys_and_resolves_default(self) -> None:
        config = validate_config(valid_config())
        self.assertEqual(["example/one"], config["repository_sets"]["primary"])
        self.assertEqual(["example/one"], resolve_repositories(config))

    def test_explicit_empty_selection_fails_without_fallback(self) -> None:
        config = validate_config(valid_config())
        with self.assertRaisesRegex(ConfigurationError, "must not be empty"):
            resolve_repositories(config, explicit=[])

    def test_unknown_security_sensitive_field_fails(self) -> None:
        config = valid_config()
        config["command"] = "run-anything"
        with self.assertRaisesRegex(ConfigurationError, "unknown field"):
            validate_config(config)

    def test_unknown_runtime_and_future_schema_fail(self) -> None:
        config = valid_config()
        config["runtime"] = "mystery"
        with self.assertRaisesRegex(
            ConfigurationFieldError,
            '^config.runtime must be one of "auto", "claude-code", "codex", "copilot-cli", found string "mystery"$',
        ):
            validate_config(config)

    def test_repository_reviewer_requires_checkout_and_safe_manifest(self) -> None:
        config = valid_config()
        reviewer = config["repositories"]["example/one"]["reviewer"]
        reviewer.update({"id": "specialist", "scope": "repository", "manifest_path": "../adapter.json"})
        with self.assertRaisesRegex(ConfigurationError, "unsafe"):
            validate_config(config)
        reviewer["manifest_path"] = ".review/adapter.json"
        config["repositories"]["example/one"]["checkout_path"] = None
        with self.assertRaisesRegex(ConfigurationError, "checkout_path"):
            validate_config(config)
        config = valid_config()
        config["schema_version"] = 99
        with self.assertRaisesRegex(ConfigurationError, "future"):
            validate_config(config)

    def test_a_repository_reviewer_is_a_committed_manifest_or_a_skill_with_an_optional_local_manifest(self) -> None:
        def reviewer_config(**fields: object) -> dict:
            config = valid_config()
            config["repositories"]["example/one"]["reviewer"] = {
                "id": "team",
                "protocol_version": 1,
                "trusted_ref": None,
                "scope": "repository",
                **fields,
            }
            return config

        for fields in (
            {"manifest_path": ".review/adapter.json"},
            {"skill": ".claude/agents/review.md"},
            {"skill": ".claude/agents/review.md", "manifest": True},
            {"skill": ".claude/agents/review.md", "manifest": native("C:\\Reviewers\\one\\manifest.json")},
        ):
            with self.subTest(fields=fields):
                reviewer = validate_config(reviewer_config(**fields))["repositories"]["example/one"]["reviewer"]
                self.assertEqual(
                    {"manifest_path", "skill", "manifest"} & set(fields),
                    {key for key in ("manifest_path", "skill", "manifest") if reviewer[key] is not None},
                )
        for invalid, message in (
            ({}, "needs skill"),
            ({"manifest": True}, "needs skill"),
            ({"manifest_path": ".review/adapter.json", "skill": "review.md"}, "cannot also set skill or manifest"),
            ({"skill": "../outside.md"}, "skill is unsafe"),
            ({"skill": "review.md", "manifest": "relative/manifest.json"}, ABSOLUTE_PATH),
            (
                {"skill": "review.md", "manifest": False},
                field_pattern(
                    "config.repositories.example/one.reviewer.manifest",
                    "true, a non-empty string, or null",
                    "boolean false",
                ),
            ),
        ):
            with self.subTest(fields=invalid), self.assertRaisesRegex(ConfigurationError, message):
                validate_config(reviewer_config(**invalid))
        generic = valid_config()
        generic["repositories"]["example/one"]["reviewer"]["skill"] = "review.md"
        with self.assertRaisesRegex(ConfigurationError, "generic scope"):
            validate_config(generic)
        self.assertEqual(
            Path("C:/Config/reviewers/example/one/manifest.json"),
            default_manifest_path(Path("C:/Config/config.json"), "Example/One"),
        )

    def test_re_review_scope_thresholds_have_defaults_and_are_validated(self) -> None:
        self.assertEqual({"full_share": 0.5, "full_lines": 1000}, validate_config(valid_config())["re_review_scope"])
        config = valid_config()
        config["re_review_scope"] = {"full_share": 0.25}
        self.assertEqual({"full_share": 0.25, "full_lines": 1000}, validate_config(config)["re_review_scope"])
        for value, message in (
            ({"full_share": 0}, "full_share must be a number above 0 and at most 1"),
            ({"full_share": 1.5}, "full_share must be a number above 0 and at most 1"),
            ({"full_share": True}, "config.re_review_scope.full_share must be a finite number, found boolean true"),
            ({"full_lines": 0}, "config.re_review_scope.full_lines must be an integer of at least 1, found integer 0"),
            (
                {"full_lines": 2.5},
                "config.re_review_scope.full_lines must be an integer of at least 1, found number 2.5",
            ),
            ({"full_files": 3}, "unknown field"),
            ([], "re_review_scope must be an object"),
        ):
            config["re_review_scope"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                validate_config(config)

    def test_write_failure_preserves_previous_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            write_config(valid_config(), path)
            previous = path.read_bytes()
            with (
                mock.patch.object(Path, "replace", side_effect=OSError("synthetic")),
                self.assertRaises(PersistenceError),
            ):
                write_config(valid_config(), path)
            self.assertEqual(previous, path.read_bytes())

    def test_dashboard_markers_and_threshold_boundaries_are_validated(self) -> None:
        config = valid_config()
        config["verdict_policy"]["should_fix_threshold"] = 1
        self.assertEqual(1, validate_config(config)["verdict_policy"]["should_fix_threshold"])
        threshold = "config.verdict_policy.should_fix_threshold"
        for invalid, found in ((0, "integer 0"), (True, "boolean true")):
            with self.subTest(threshold=invalid):
                config = valid_config()
                config["verdict_policy"]["should_fix_threshold"] = invalid
                with self.assertRaisesRegex(
                    ConfigurationFieldError, field_pattern(threshold, "an integer of at least 1", found)
                ):
                    validate_config(config)
        marker = "a non-empty trimmed single-line string"
        markers: tuple[tuple[str, str, str | None], ...] = (
            ("", "end", "an empty string"),
            ("   ", "end", "a blank string"),
            ("same", "same", None),
            ("start\nsecond-line", "end", "a string of more than one line"),
            (" start", "end", "a string with whitespace around it"),
        )
        for start, end, start_found in markers:
            with self.subTest(start=start, end=end):
                config = valid_config()
                config["dashboard"] = {"start_marker": start, "end_marker": end}
                pattern = "^dashboard markers must be distinct$"
                if start_found is not None:
                    pattern = field_pattern("config.dashboard.start_marker", marker, start_found)
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    validate_config(config)

    def test_dashboard_overrides_cannot_duplicate_computed_states(self) -> None:
        config = valid_config()
        config["dashboard"] = {"status_overrides": {"example/one#12": "stale"}}
        with self.assertRaisesRegex(ConfigurationError, "computed tracker state"):
            validate_config(config)
        config["dashboard"]["status_overrides"]["example/one#12"] = "on hold"
        normalized = validate_config(config)
        self.assertEqual(
            "on hold",
            normalized["dashboard"]["status_overrides"]["example/one#12"],
        )

    def test_dashboard_author_names_default_to_empty_and_are_trimmed(self) -> None:
        self.assertEqual({}, validate_config(valid_config())["dashboard"]["author_names"])
        config = valid_config()
        config["dashboard"] = {"author_names": {"Ada-L": "  Ada Lovelace ", "bob": "Bob"}}
        self.assertEqual(
            {"Ada-L": "Ada Lovelace", "bob": "Bob"},
            validate_config(config)["dashboard"]["author_names"],
        )

    def test_dashboard_author_names_are_validated(self) -> None:
        for names, message in (
            (["ada"], "must be an object"),
            ({"-ada": "Ada"}, "Invalid dashboard author name login"),
            ({"ada/lovelace": "Ada"}, "Invalid dashboard author name login"),
            ({"a" * 40: "Ada"}, "Invalid dashboard author name login"),
            ({"ada": "Ada", "ADA": "Ada"}, "Duplicate dashboard author name login"),
            ({"ada": ""}, "non-empty single-line"),
            ({"ada": "   "}, "non-empty single-line"),
            ({"ada": "Ada\nLovelace"}, "non-empty single-line"),
            ({"ada": "Ada\rLovelace"}, "non-empty single-line"),
            ({"ada": None}, "non-empty single-line"),
            ({"ada": 7}, "non-empty single-line"),
        ):
            with self.subTest(names=names):
                config = valid_config()
                config["dashboard"] = {"author_names": names}
                with self.assertRaisesRegex(ConfigurationError, message):
                    validate_config(config)

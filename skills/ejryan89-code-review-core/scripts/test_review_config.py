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
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_presence
import review_config
import review_flags
from executable_locator import RelativeEnvironmentPath
from git_client import GitError, GitResult
from review_config import (
    ConfigurationError,
    ConfigurationFieldError,
    default_manifest_path,
    resolve_repositories,
    validate_config,
    write_config,
)
from review_io import PersistenceError
from review_state import default_state_path
from test_review_config_validation import ABSOLUTE_PATH, CONTAINER_CASES, _config, native
from test_review_io import UNPARSEABLE_JSON
from test_review_records import field_pattern

SCRIPT_DIRECTORY = Path(__file__).resolve().parent


def valid_config() -> dict[str, Any]:
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

    def test_a_failure_holding_a_line_break_is_one_failed_line(self) -> None:
        # An OSError's text names the path it could not read, which may hold a line break of its own.
        path = self.write_json("config.json", valid_config())
        refusal = ConfigurationError("Cannot read\nVALID elsewhere\u2028at\tall")
        with mock.patch.object(review_config, "load_config", side_effect=refusal):
            self.assertEqual(
                (1, "FAILED Cannot read VALID elsewhere at all\n", ""), self.run_main("validate", str(path))
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

    def test_write_reads_a_candidate_after_a_byte_order_mark(self) -> None:
        # An editor on Windows may save the candidate with a UTF-8 byte-order mark, which the reader skips.
        candidate = self.root / "candidate.json"
        candidate.write_bytes(b"\xef\xbb\xbf" + json.dumps(valid_config()).encode("utf-8"))
        output = self.root / "config.json"
        self.assertEqual((0, f"WROTE {output}\n", ""), self.run_main("write", str(candidate), "--output", str(output)))

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
        cases: list[tuple[Path, str]] = [
            (
                self.write_json("invalid.json", {**valid_config(), "runtime": "nowhere"}),
                'config.runtime must be one of "auto", "claude-code", "codex", "copilot-cli", found string "nowhere"',
            ),
            (broken, "Expecting property name enclosed in double quotes"),
            (undecodable, "can't decode byte 0xe9"),
            (self.root / "absent.json", "absent.json"),
        ]
        for index, content in enumerate(UNPARSEABLE_JSON.values()):
            (unparseable := self.root / f"unparseable {index}.json").write_bytes(content)
            cases.append((unparseable, ""))  # its reason is the parser's own, which only has to end as a FAILED line
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
        def reviewer_config(**fields: object) -> dict[str, Any]:
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

    def test_a_default_manifest_folder_windows_would_share_or_open_as_a_device_is_refused_by_name(self) -> None:
        # The folder an earlier release found a hand-written manifest in is named, never read.
        reviewers = Path("C:/Config/reviewers")
        for identity, folder, reason in (
            ("Owner/a.", reviewers / "owner" / "a.", "trims the trailing dots of 'a.'"),
            ("owner/NUL.js", reviewers / "owner" / "nul.js", "opens as a device 'nul.js'"),
            ("con/a", reviewers / "con" / "a", "opens as a device 'con'"),
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(ConfigurationError) as refused:
                    default_manifest_path(Path("C:/Config/config.json"), identity)
                self.assertEqual(
                    f"Repository {identity.lower()} cannot have a folder of its own: Windows {reason}, so it is "
                    f"refused on every platform; {folder} is neither read nor written",
                    str(refused.exception),
                )

    def test_unread_source_lines_defaults_to_200_and_takes_a_positive_integer(self) -> None:
        self.assertEqual(200, validate_config(valid_config())["unread_source_lines"])
        self.assertEqual(1, validate_config({**valid_config(), "unread_source_lines": 1})["unread_source_lines"])
        for value, found in ((0, "integer 0"), (2.5, "number 2.5"), (None, "null"), ("200", "string")):
            with self.subTest(value=value), self.assertRaises(ConfigurationError) as raised:
                validate_config({**valid_config(), "unread_source_lines": value})
            self.assertIn(
                f"config.unread_source_lines must be an integer of at least 1, found {found}", str(raised.exception)
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

    def test_dashboard_ignored_repositories_default_to_empty_and_are_lowercased(self) -> None:
        self.assertEqual([], validate_config(valid_config())["dashboard"]["ignored_repositories"])
        config = valid_config()
        config["dashboard"] = {"ignored_repositories": ["Other/Sandbox", "octo/test-*", "*/scratch?"]}
        self.assertEqual(
            ["other/sandbox", "octo/test-*", "*/scratch?"],
            validate_config(config)["dashboard"]["ignored_repositories"],
        )

    def test_dashboard_ignored_repositories_are_validated(self) -> None:
        for entries, message in (
            ("other/sandbox", "must be an array"),
            ([7], "must be a non-empty string"),
            (["other"], "an owner/repo identity, or a pattern"),
            (["other/one/two"], "an owner/repo identity, or a pattern"),
            (["other/[ab]"], "an owner/repo identity, or a pattern"),
            (["other/"], "an owner/repo identity, or a pattern"),
            (["other/a b"], "an owner/repo identity, or a pattern"),
            (["other/" + "a" * 101], "an owner/repo identity, or a pattern"),
            (["other/one", "Other/One"], "distinct entries, ignoring case"),
            ([f"other/r{index}" for index in range(1001)], "at most 1000 items"),
            # A repository a set tracks, matched exactly or by a pattern, ignoring case.
            (["EXAMPLE/one"], "Repository example/one in repository set primary matches"),
            (["example/*"], "entry example/\\*; a tracked repository cannot be ignored"),
            (["*/on?"], "matches config.dashboard.ignored_repositories entry \\*/on\\?"),
        ):
            with self.subTest(entries=entries):
                config = valid_config()
                config["dashboard"] = {"ignored_repositories": entries}
                with self.assertRaisesRegex(ConfigurationError, message):
                    validate_config(config)
        # A pattern matches the owner and the name each on their own, so `*` never spans the slash.
        config = valid_config()
        config["dashboard"] = {"ignored_repositories": ["exam*one/*", "example/x*", "e*/one?"]}
        validate_config(config)

    def test_a_repository_identity_holds_githubs_name_rule_and_every_reader_built_on_it_agrees(self) -> None:
        # GitHub's repository name rule: up to 100 letters, digits, `_`, `.`, and `-`, any of them first, as in an
        # organization's `.github`, except a name made only of dots: GitHub refuses `.` and `..`, and the rule
        # refuses every longer run, which a Windows archive path would not name as written. The owner is a login by
        # github_logins' rule, letters and digits with single hyphens inside, at most one `_` before a managed user's
        # short code, and at most 39 characters, or review_config.LOCAL_OWNER, which no login can be.
        accepted = [
            "owner/.github",
            "owner/a.b",
            "owner/.git",
            "owner/a..",
            "owner/..a",
            "owner/-repo",
            "owner/_repo",
            "Owner/.GitHub",
            "o/" + "n" * 100,
            "o" * 39 + "/n",
            "a_b/n",
            "a-b-c/n",
            "review-document.local/n",
        ]
        refused = [
            "a.b/n",
            "a_b_c/n",
            "a--b/n",
            "a-/n",
            "_a/n",
            "o" * 40 + "/n",
            "review-document.locals/n",
            "x.review-document.local/n",
            # An owner ending in a dot, which Windows would trim, is no login, so no archive folder is built from one.
            "owner./a",
            "owner/.",
            "owner/..",
            "owner/...",
            "owner/....",
            "owner/",
            ".owner/repo",
            "-owner/repo",
            "owner",
            "owner/repo/x",
            "owner/./x",
            "owner/repo name",
            "owner/repo\n",
            "o/" + "n" * 101,
            "o" * 101 + "/n",
        ]
        for identity in accepted:
            with self.subTest(identity=identity):
                self.assertEqual(identity.lower(), review_config.validate_repository_identity(identity))
                # A mute names any identity the check reports, and matches it.
                entry = review_config.normalize_ignored_repository(identity, "entry")
                self.assertEqual(entry, review_config.ignored_repository_match(identity, [entry]))
                # A status override's key is the identity and a pull request's number.
                config = valid_config()
                config["dashboard"] = {"status_overrides": {f"{identity}#1": "On hold"}}
                validate_config(config)
        for identity in refused:
            with (
                self.subTest(identity=identity),
                self.assertRaisesRegex(ConfigurationError, "^Invalid repository identity: "),
            ):
                review_config.validate_repository_identity(identity)

    def test_a_status_override_key_an_earlier_release_accepted_still_loads(self) -> None:
        # The previous release's key check took any run of letters, digits, `_`, `.`, and `-` on each side of the
        # slash, and its `override --set` wrote what it took; the next release loads every such configuration, since
        # every code-review skill reads it and `override` itself validates the file before it edits it.
        for key in (
            "-owner/repo#1",
            ".owner/repo#1",
            "_owner/repo#1",
            "owner/.#1",
            "owner/..#1",
            "o/" + "n" * 101 + "#1",
            "owner/repo#1234567890",
        ):
            with self.subTest(key=key):
                config = valid_config()
                config["dashboard"] = {"status_overrides": {key: "On hold"}}
                validate_config(config)
        # A number past ten digits is no pull request update-pr-dashboard reads, so `update` and `override --clear`
        # would refuse the key the configuration accepted.
        for key in (
            "owner/repo",
            "owner#1",
            "owner/repo/x#1",
            "owner/re po#1",
            "owner/repo#0",
            "/repo#1",
            "owner/repo#12345678901",
        ):
            with self.subTest(key=key):
                config = valid_config()
                config["dashboard"] = {"status_overrides": {key: "On hold"}}
                with self.assertRaisesRegex(ConfigurationError, "^Invalid dashboard status override key: "):
                    validate_config(config)

    def test_an_ignored_entry_splits_owner_from_name_at_the_first_slash(self) -> None:
        match = review_config.ignored_repository_match
        self.assertEqual("octo/*", match("Octo/Sandbox", ["other/*", "octo/*"]))
        self.assertIsNone(match("octo/sandbox", ["octo/test-*", "sandbox/*"]))
        # Everything after the first slash is the name, in the identity and in the entry alike.
        self.assertEqual("a/*", match("a/b/c", ["a/*"]))
        self.assertEqual("a/b/*", match("a/b/c", ["a/b/*"]))
        self.assertIsNone(match("a/b/c", ["a/b"]))

    def test_each_ignored_repositories_refusal_names_the_field_and_what_it_found(self) -> None:
        field = "config.dashboard.ignored_repositories"
        for entries, message in (
            ("x", f'{field} must be an array of at most 1000 items, found string "x"'),
            ([7], f"{field}[0] must be a non-empty string, found integer 7"),
            (
                ["a/b", "other"],
                f"{field}[1] must be an owner/repo identity, or a pattern of one using only * and ? as wildcards, "
                'found string "other"',
            ),
            (["a/b", "A/B"], f'{field} must be distinct entries, ignoring case, found "a/b" twice'),
        ):
            with self.subTest(entries=entries):
                config = valid_config()
                config["dashboard"] = {"ignored_repositories": entries}
                with self.assertRaises(ConfigurationFieldError) as raised:
                    validate_config(config)
                self.assertEqual(message, str(raised.exception))

    def test_the_default_configuration_file_is_named_config_json(self) -> None:
        # update-pr-dashboard names its tracker record after this name, so a rename would move the record.
        self.assertEqual("config.json", review_config.DEFAULT_CONFIG_NAME)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"CODE_REVIEW_CONFIG": ""}):
            with mock.patch.object(Path, "home", return_value=Path(home)):
                path = review_config.default_config_path()
            self.assertEqual(".coding-agent-skills/code-review/config.json", path.relative_to(home).as_posix())

    def test_each_default_store_lies_under_the_home_s_code_review_folder(self) -> None:
        # The stores move with these names, so a user's configuration, flags, and state would be left behind.
        overrides = {"CODE_REVIEW_CONFIG": "", "CODE_REVIEW_FLAGS": "", "CODE_REVIEW_STATE": ""}
        for default, name in (
            (review_config.default_config_path, "config.json"),
            (review_flags.default_flags_path, "flags.json"),
            (default_state_path, "state.json"),
        ):
            with (
                self.subTest(default=default.__name__),
                tempfile.TemporaryDirectory() as home,
                mock.patch.dict(os.environ, overrides),
                mock.patch.object(Path, "home", return_value=Path(home)),
            ):
                path = default()
                self.assertEqual(f".coding-agent-skills/code-review/{name}", path.relative_to(home).as_posix())

    def test_each_default_store_refuses_a_relative_home_rather_than_read_under_the_working_directory(self) -> None:
        # Path.home() reads a relative profile folder as given, so each default store named a file under the working
        # directory.
        overrides = {"CODE_REVIEW_CONFIG": "", "CODE_REVIEW_FLAGS": "", "CODE_REVIEW_STATE": ""}
        for default in (review_config.default_config_path, review_flags.default_flags_path, default_state_path):
            with (
                self.subTest(default=default.__name__),
                mock.patch.dict(os.environ, overrides),
                mock.patch.object(Path, "home", return_value=Path("relative home")),
                self.assertRaises(RelativeEnvironmentPath),
            ):
                default()

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


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ReviewerRootTests(unittest.TestCase):
    """A reviewer read from another checkout: validate and write refuse a root that is missing, not a folder, not a
    Git checkout's top folder, or whose trusted ref does not resolve there, naming the repository, and write then
    writes nothing."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "reviewer root"
        self.rules = self.root / "conventions repo"
        self.rules.mkdir(parents=True)
        git(self.rules, "init", "-b", "main")
        (self.rules / "review").mkdir()
        (self.rules / "review" / "python.md").write_text("Python profile\n", encoding="utf-8")
        git(self.rules, "add", ".")
        git(self.rules, "commit", "-m", "rules")
        self.commit = git(self.rules, "rev-parse", "HEAD")

    def config(self, root: Path | str, ref: str = "refs/heads/main") -> dict[str, Any]:
        config = valid_config()
        config["repositories"]["example/one"]["reviewer"] = {
            "id": "conventions",
            "protocol_version": 1,
            "scope": "repository",
            "skill": "review/python.md",
            "trusted_ref": ref,
            "reviewer_root": str(root),
        }
        return config

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review_config._main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def assert_refused(self, config: dict[str, Any], reason: str) -> None:
        """Both commands print the reason as one FAILED line, and write leaves no configuration behind."""
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps(config), encoding="utf-8")
        output = self.root / "written.json"
        expected = f"FAILED repositories.example/one.reviewer: {reason}"
        for arguments in (("validate", str(candidate)), ("write", str(candidate), "--output", str(output))):
            with self.subTest(command=arguments[0]):
                code, out, err = self.run_main(*arguments)
                self.assertEqual((1, ""), (code, err))
                self.assertTrue(out.startswith(expected), out)
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertFalse(output.exists())

    def test_a_checkout_whose_ref_resolves_is_valid_and_written(self) -> None:
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps(self.config(self.rules)), encoding="utf-8")
        output = self.root / "written.json"
        self.assertEqual((0, f"VALID {candidate}\n", ""), self.run_main("validate", str(candidate)))
        self.assertEqual((0, f"WROTE {output}\n", ""), self.run_main("write", str(candidate), "--output", str(output)))
        reviewer = json.loads(output.read_text(encoding="utf-8"))["repositories"]["example/one"]["reviewer"]
        self.assertEqual((str(self.rules), "refs/heads/main"), (reviewer["reviewer_root"], reviewer["trusted_ref"]))
        self.assertEqual(self.commit, review_config.reviewer_root_commit(self.rules, "refs/heads/main"))

    def test_the_commit_is_the_ref_s_never_the_working_tree_s(self) -> None:
        # A branch checked out in the root, with uncommitted edits, does not move the trusted ref's commit.
        git(self.rules, "switch", "-c", "elsewhere")
        (self.rules / "review" / "python.md").write_text("Edited\n", encoding="utf-8")
        git(self.rules, "commit", "-am", "elsewhere")
        (self.rules / "review" / "python.md").write_text("Uncommitted\n", encoding="utf-8")
        self.assertEqual(self.commit, review_config.reviewer_root_commit(self.rules, "refs/heads/main"))

    def test_a_missing_root_is_refused(self) -> None:
        absent = self.root / "absent"
        self.assert_refused(self.config(absent), f"reviewer_root {absent} does not exist")

    def test_a_file_in_place_of_the_root_is_refused(self) -> None:
        file = self.root / "file"
        file.write_text("not a folder\n", encoding="utf-8")
        self.assert_refused(self.config(file), f"reviewer_root {file} is not a folder")

    def test_a_root_that_cannot_be_examined_is_refused(self) -> None:
        with mock.patch.object(file_presence, "is_directory", side_effect=PermissionError("denied")):
            self.assert_refused(self.config(self.rules), f"reviewer_root {self.rules} cannot be read: denied")

    def test_a_folder_that_is_not_a_git_checkout_is_refused(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assert_refused(self.config(plain), f"reviewer_root {plain} is not a Git checkout git can read: ")

    def test_a_folder_inside_a_checkout_is_refused(self) -> None:
        # git would read the checkout around it, which is not the folder configured.
        inner = self.rules / "review"
        self.assert_refused(
            self.config(inner), f"reviewer_root {inner} is not a Git checkout's top folder; its checkout is "
        )

    def test_a_ref_that_does_not_resolve_is_refused(self) -> None:
        for ref in ("refs/heads/absent", "refs/tags/absent", "refs/remotes/origin/absent"):
            with self.subTest(ref=ref):
                self.assert_refused(
                    self.config(self.rules, ref),
                    f"trusted_ref {ref} does not resolve to a commit in reviewer_root {self.rules}",
                )

    def test_a_ref_git_resolves_into_a_pull_request_is_refused(self) -> None:
        # A symbolic ref under refs/heads/ that git follows to a pull request's ref is judged where it lands.
        git(self.rules, "update-ref", "refs/pull/42/head", "HEAD")
        git(self.rules, "symbolic-ref", "refs/heads/sym", "refs/pull/42/head")
        self.assert_refused(
            self.config(self.rules, "refs/heads/sym"),
            f"trusted_ref refs/heads/sym resolves to refs/pull/42/head, which is not a ref under refs/heads/, "
            f"refs/tags/, refs/remotes/ in reviewer_root {self.rules}",
        )

    def test_a_name_git_check_ref_format_refuses_is_refused(self) -> None:
        for ref in ("refs/heads/.hidden", "refs/heads/main.lock", "refs/heads//main", "refs/heads/main/"):
            with self.subTest(ref=ref):
                self.assert_refused(
                    self.config(self.rules, ref), f"trusted_ref {ref} is not a ref name git check-ref-format accepts"
                )

    def test_each_spelling_that_reaches_a_pull_request_is_refused_by_the_root_s_reader(self) -> None:
        # Through reviewer_root_commit, as prepare and validate-reviewer --ref call it: every spelling names a commit a
        # pull request's ref reaches here, and none is read.
        git(self.rules, "commit", "--allow-empty", "-m", "pull request")
        git(self.rules, "update-ref", "refs/pull/42/head", "HEAD")
        git(self.rules, "update-ref", "refs/pull/42/merge", "HEAD")
        git(self.rules, "reset", "--hard", "HEAD~1")
        git(self.rules, "symbolic-ref", "refs/heads/sym", "refs/pull/42/head")
        pull = git(self.rules, "rev-parse", "refs/pull/42/head")
        outside = "is not a full ref under refs/heads/, refs/tags/, refs/remotes/"
        syntax = "holds revision syntax, such as ~, ^, :, or @{, so it names a commit other than the ref's own"
        for ref, reason in (
            ("pull/42/head~1", outside),
            ("pull/42/head^", outside),
            ("pull/42/merge", outside),
            ("pull/42/head", outside),
            ("Refs/pull/42/head", outside),
            ("refs/pull/42/head", outside),
            (pull, outside),
            ("main", outside),
            ("refs/heads/sym~1", syntax),
            ("refs/heads/sym^", syntax),
            ("refs/heads/main@{0}", syntax),
        ):
            with self.subTest(ref=ref), self.assertRaises(ConfigurationError) as caught:
                review_config.reviewer_root_commit(self.rules, ref)
            self.assertEqual(f"trusted_ref {json.dumps(ref)} {reason}", str(caught.exception))

    def test_a_remote_tracking_head_and_a_tag_resolve_to_their_commits(self) -> None:
        git(self.rules, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.rules, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git(self.rules, "tag", "-a", "v1", "-m", "release")
        for ref in ("refs/remotes/origin/HEAD", "refs/tags/v1"):
            with self.subTest(ref=ref):
                self.assertEqual(self.commit, review_config.reviewer_root_commit(self.rules, ref))

    def test_git_that_cannot_run_or_prints_no_top_folder_is_refused(self) -> None:
        def missing(command: Any, timeout: float) -> GitResult:
            raise GitError("git is not installed")

        def silent(command: Any, timeout: float) -> GitResult:
            return GitResult(0, "", "")

        def broken_ref(command: Any, timeout: float) -> GitResult:
            # The root is a checkout, and git fails reading the ref for a reason of its own, not an absent ref.
            if "--show-toplevel" in command:
                return GitResult(0, f"{self.rules.as_posix()}\n", "")
            return GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")

        with self.assertRaisesRegex(ConfigurationError, "is not a Git checkout git can read: git is not installed"):
            review_config.reviewer_root_commit(self.rules, "refs/heads/main", missing)
        with self.assertRaisesRegex(ConfigurationError, "is not a Git checkout git can read: it named no top folder"):
            review_config.reviewer_root_commit(self.rules, "refs/heads/main", silent)
        with self.assertRaisesRegex(
            ConfigurationError, "trusted_ref refs/heads/main cannot be read in reviewer_root .*: .*bad config line 1"
        ):
            review_config.reviewer_root_commit(self.rules, "refs/heads/main", broken_ref)

    def test_only_reviewers_with_a_root_are_checked(self) -> None:
        # The generic reviewer and a repository reviewer read from its own checkout start no git.
        def refuse(command: Any, timeout: float) -> GitResult:
            raise AssertionError(f"unexpected git call: {command}")

        config = valid_config()
        config["repositories"]["example/two"] = {
            "reviewer": {
                "id": "own",
                "protocol_version": 1,
                "skill": "review/SKILL.md",
                "trusted_ref": "refs/heads/main",
            },
            "checkout_path": native("C:\\Repos\\Two"),
        }
        config["repository_sets"]["primary"].append("example/two")
        review_config.check_reviewer_roots(validate_config(config), refuse)


class LocalManifestTests(unittest.TestCase):
    """A reviewer's local manifest, whose folder holds the condition scripts a review runs, lies outside the reviewed
    repository: validate and write refuse one inside checkout_path or inside any working tree of the same repository,
    naming the path, and write then writes nothing."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "manifest root"
        self.checkout = self.root / "reviewed checkout"
        self.checkout.mkdir(parents=True)
        git(self.checkout, "init", "-b", "main")
        (self.checkout / "README.md").write_text("Reviewed\n", encoding="utf-8")
        git(self.checkout, "add", ".")
        git(self.checkout, "commit", "-m", "reviewed")
        self.worktree = self.root / "other worktree"
        git(self.checkout, "worktree", "add", "-b", "other", str(self.worktree))
        self.common = Path(git(self.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    def config(self, manifest: Path | bool, checkout: Path | None = None) -> dict[str, Any]:
        config = valid_config()
        config["repositories"]["example/one"] = {
            "reviewer": {
                "id": "team",
                "protocol_version": 1,
                "scope": "repository",
                "skill": "review/SKILL.md",
                "manifest": manifest if manifest is True else str(manifest),
            },
            "checkout_path": str(checkout or self.checkout),
        }
        return config

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review_config._main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def commands(self, config: dict[str, Any], candidate: Path) -> list[tuple[str, tuple[int, str, str], Path]]:
        """Each command's result for `config` written to `candidate`, with the file write was to write."""
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(json.dumps(config), encoding="utf-8")
        output = candidate.with_name("written.json")
        return [
            ("validate", self.run_main("validate", str(candidate)), output),
            ("write", self.run_main("write", str(candidate), "--output", str(output)), output),
        ]

    def assert_refused(self, config: dict[str, Any], reason: str, candidate: Path | None = None) -> None:
        expected = f"FAILED repositories.example/one.reviewer: {reason}\n"
        for command, result, output in self.commands(config, candidate or self.root / "candidate.json"):
            with self.subTest(command=command):
                self.assertEqual((1, expected, ""), result)
                self.assertFalse(output.exists())

    def assert_accepted(self, config: dict[str, Any], candidate: Path | None = None) -> None:
        for command, (code, out, err), _ in self.commands(config, candidate or self.root / "candidate.json"):
            with self.subTest(command=command):
                self.assertEqual((0, ""), (code, err), out)

    def inside(self, manifest: Path, checkout: Path | None = None) -> str:
        return (
            f"manifest {manifest} lies inside checkout_path {checkout or self.checkout}; keep it outside the repository"
        )

    def in_worktree(self, manifest: Path, checkout: Path | None = None) -> str:
        return (
            f"manifest {manifest} lies inside a working tree of the repository checkout_path "
            f"{checkout or self.checkout} holds, whose Git directory is {self.common}; keep it outside the repository"
        )

    def test_a_manifest_inside_the_checkout_is_refused(self) -> None:
        for manifest in (
            self.checkout / ".review" / "manifest.json",
            self.checkout / "manifest.json",
            self.checkout / ".git" / "review" / "manifest.json",
            self.checkout / "not yet" / "made" / "manifest.json",
        ):
            with self.subTest(manifest=manifest):
                self.assert_refused(self.config(manifest), self.inside(manifest))

    def test_a_manifest_inside_another_working_tree_of_the_repository_is_refused(self) -> None:
        for manifest in (self.worktree / ".review" / "manifest.json", self.worktree / "not yet" / "manifest.json"):
            with self.subTest(manifest=manifest):
                self.assert_refused(self.config(manifest), self.in_worktree(manifest))
        # The same holds from a linked worktree's side: the main checkout shares its Git directory.
        manifest = self.checkout / "review" / "manifest.json"
        self.assert_refused(self.config(manifest, self.worktree), self.in_worktree(manifest, self.worktree))

    def test_the_default_manifest_beside_a_configuration_inside_the_checkout_is_refused(self) -> None:
        candidate = self.checkout / "config" / "config.json"
        manifest = self.checkout / "config" / "reviewers" / "example" / "one" / "manifest.json"
        self.assert_refused(self.config(True), self.inside(manifest), candidate)
        self.assert_accepted(self.config(True), self.root / "outside" / "config.json")

    def test_a_manifest_outside_the_repository_is_accepted(self) -> None:
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        git(unrelated, "init", "-b", "main")
        for manifest in (
            self.root / "manifests" / "manifest.json",
            unrelated / "review" / "manifest.json",
            self.root / "reviewed checkout sibling" / "manifest.json",
        ):
            with self.subTest(manifest=manifest):
                self.assert_accepted(self.config(manifest))

    def test_a_checkout_not_yet_cloned_is_compared_by_path_alone(self) -> None:
        # A checkout_path git cannot be asked about yet still refuses a manifest inside it, and the review compares
        # Git directories once the checkout is there.
        absent = self.root / "absent checkout"
        self.assert_accepted(self.config(self.worktree / "manifest.json", absent))
        self.assert_refused(
            self.config(absent / "manifest.json", absent),
            self.inside(absent / "manifest.json", absent),
            self.root / "refused" / "candidate.json",
        )

    def test_git_or_a_folder_that_cannot_be_examined_refuses_the_manifest(self) -> None:
        manifest = self.root / "manifests" / "manifest.json"
        config = validate_config(self.config(manifest))

        def missing(command: Any, timeout: float) -> GitResult:
            raise GitError("git is not installed")

        prefix = f"repositories.example/one.reviewer: manifest {manifest} cannot be checked against checkout_path "
        with self.assertRaises(ConfigurationError) as caught:
            review_config.check_local_manifests(config, self.root / "config.json", missing)
        self.assertEqual(f"{prefix}{self.checkout}: git is not installed", str(caught.exception))
        with (
            mock.patch.object(file_presence, "is_directory", side_effect=PermissionError("denied")),
            self.assertRaises(ConfigurationError) as caught,
        ):
            review_config.check_local_manifests(config, self.root / "config.json")
        self.assertEqual(f"{prefix}{self.checkout}: denied", str(caught.exception))

    def test_only_reviewers_with_a_local_manifest_are_checked(self) -> None:
        # The generic reviewer, a skill run alone, and a default manifest no folder can hold start no git.
        def refuse(command: Any, timeout: float) -> GitResult:
            raise AssertionError(f"unexpected git call: {command}")

        config = valid_config()
        for identity, reviewer in (
            ("example/two", {"id": "own", "protocol_version": 1, "skill": "review/SKILL.md"}),
            ("example/con", {"id": "own", "protocol_version": 1, "skill": "review/SKILL.md", "manifest": True}),
        ):
            config["repositories"][identity] = {"reviewer": reviewer, "checkout_path": str(self.checkout)}
            config["repository_sets"]["primary"].append(identity)
        review_config.check_local_manifests(validate_config(config), self.root / "config.json", refuse)

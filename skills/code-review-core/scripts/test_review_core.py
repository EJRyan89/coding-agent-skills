from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_github
import review_io
import review_runtime
from review_archive import ArchiveError, commit_record, latest_record, list_versions, pull_directory
from review_config import (
    ConfigurationError,
    default_manifest_path,
    resolve_repositories,
    validate_config,
    write_config,
)
from review_flags import FlagError, add_flag, load_store, resolve_flag
from review_github import CommandResult, GitHubClient, GitHubError
from review_hosts import (
    HostSuperseded,
    ProcessResult,
    copilot_command,
    find_copilot,
    parse_copilot_version,
    run_copilot,
)
from review_io import PersistenceError, ResourceLock, atomic_write_json, map_in_order, read_json
from review_operation import (
    ReviewOperationError,
    commit_adapter_result,
    latest_reviewed_heads,
    parse_pull_selector,
    safe_watermark,
    select_eligible_pulls,
    validate_canary_pull,
)
from review_process import ProcessStatus
from review_records import (
    TITLE_MAXIMUM_LENGTH,
    RecordError,
    build_record,
    calculate_verdict,
    render_markdown,
    valid_analyzer,
    valid_title,
    validate_adapter_result,
    validate_record,
    validate_record_pair,
    write_record_pair,
)
from review_runtime import (
    SOURCE_SNAPSHOT_MANIFEST,
    RuntimeContractError,
    build_adapter_request,
    load_manifest_from_commit,
    materialize_reviewer,
    materialize_source_snapshot,
    negotiate_capabilities,
    resolve_reviewer_commit,
    resolve_runtime,
    validate_adapter_manifest,
    verify_checkout_remote,
    verify_source_snapshot,
)
from review_state import StateError, empty_state, load_state, update_state

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
                "checkout_path": "C:\\Repos\\One",
            }
        },
        "archive_root": "C:\\Reviews\\Archive",
        "local_mirror_root": "C:\\Reviews\\Mirror",
        "summary_root": "C:\\Reviews\\Summaries",
        "dashboard_file": "C:\\Reviews\\Dashboard.md",
        "github_login": "reviewer",
        "runtime": "auto",
        "verdict_policy": {
            "request_changes_for": ["MUST_FIX"],
            "should_fix_threshold": 3,
        },
        "dashboard": {},
    }


def valid_request(repository: str = "example/one", number: int = 12) -> dict:
    return {
        "repository": repository,
        "pull_number": number,
        "pull_url": f"https://github.com/{repository}/pull/{number}",
        "title": "Improve behavior",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "mode": "initial",
        "adapter": {
            "name": "generic",
            "scope": "generic",
            "source_commit": None,
            "source_hashes": {},
        },
    }


def valid_adapter_result(repository: str = "example/one", number: int = 12) -> dict:
    return {
        "protocol_version": 1,
        "repository": repository,
        "pull_number": number,
        "head_sha": "b" * 40,
        "summary": "One actionable issue was found.",
        "reviewer": "fixture-reviewer",
        "status": "complete",
        "findings": [
            {
                "candidate_key": "candidate-a",
                "severity": "SHOULD_FIX",
                "category": "Correctness",
                "path": "src/file.cs",
                "line": 42,
                "body": "Handle the boundary condition.",
                "evidence": "The added branch excludes zero.",
                "source": "generic",
            }
        ],
        "prior_dispositions": [],
        "usage": None,
    }


class ConfigurationTests(unittest.TestCase):
    def test_model_names_map_model_identifiers_to_display_names(self) -> None:
        self.assertEqual({}, validate_config(valid_config())["model_names"])
        arn = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc"
        config = validate_config({**valid_config(), "model_names": {arn: "  Opus 5.5 "}})
        self.assertEqual({arn: "Opus 5.5"}, config["model_names"])
        for value, message in (
            ([], "model_names must be an object"),
            ({"": "Opus"}, "model_names key"),
            ({"a\nb": "Opus"}, "model_names key"),
            ({"m" * 201: "Opus"}, "model_names key"),
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
        with self.assertRaisesRegex(ConfigurationError, "Unknown runtime"):
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
            {"skill": ".claude/agents/review.md", "manifest": "C:\\Reviewers\\one\\manifest.json"},
        ):
            with self.subTest(fields=fields):
                reviewer = validate_config(reviewer_config(**fields))["repositories"]["example/one"]["reviewer"]
                self.assertEqual(
                    {"manifest_path", "skill", "manifest"} & set(fields),
                    {key for key in ("manifest_path", "skill", "manifest") if reviewer[key] is not None},
                )
        for fields, message in (
            ({}, "needs skill"),
            ({"manifest": True}, "needs skill"),
            ({"manifest_path": ".review/adapter.json", "skill": "review.md"}, "cannot also set skill or manifest"),
            ({"skill": "../outside.md"}, "skill is unsafe"),
            ({"skill": "review.md", "manifest": "relative/manifest.json"}, "absolute drive-letter path"),
            ({"skill": "review.md", "manifest": False}, "absolute Windows path"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(ConfigurationError, message):
                validate_config(reviewer_config(**fields))
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
            ({"full_share": True}, "full_share must be a number above 0 and at most 1"),
            ({"full_lines": 0}, "full_lines must be a positive integer"),
            ({"full_lines": 2.5}, "full_lines must be a positive integer"),
            ({"full_files": 3}, "unknown field"),
            ([], "re_review_scope must be an object"),
        ):
            config["re_review_scope"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ConfigurationError, message):
                validate_config(config)

    def test_write_failure_preserves_previous_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            write_config(valid_config(), path)
            previous = path.read_bytes()
            with (
                mock.patch("review_io.os.replace", side_effect=OSError("synthetic")),
                self.assertRaises(PersistenceError),
            ):
                write_config(valid_config(), path)
            self.assertEqual(previous, path.read_bytes())

    def test_dashboard_markers_and_threshold_boundaries_are_validated(self) -> None:
        config = valid_config()
        config["verdict_policy"]["should_fix_threshold"] = 1
        self.assertEqual(1, validate_config(config)["verdict_policy"]["should_fix_threshold"])
        for invalid in (0, True):
            with self.subTest(threshold=invalid):
                config = valid_config()
                config["verdict_policy"]["should_fix_threshold"] = invalid
                with self.assertRaisesRegex(ConfigurationError, "positive integer"):
                    validate_config(config)
        for start, end in (
            ("", "end"),
            ("   ", "end"),
            ("same", "same"),
            ("start\nsecond-line", "end"),
            (" start", "end"),
        ):
            with self.subTest(start=start, end=end):
                config = valid_config()
                config["dashboard"] = {"start_marker": start, "end_marker": end}
                with self.assertRaisesRegex(ConfigurationError, "dashboard markers"):
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


class ParallelCallTests(unittest.TestCase):
    def test_results_keep_item_order_and_at_most_four_run_at_once(self) -> None:
        active = peak = 0
        guard = threading.Lock()

        def call(item: int) -> int:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05 * (10 - item))  # later items finish first
            with guard:
                active -= 1
            return item * 10

        self.assertEqual([(item * 10, None) for item in range(10)], map_in_order(call, range(10)))
        self.assertEqual(4, peak)

    def test_caught_errors_stay_with_their_item(self) -> None:
        def call(item: int) -> int:
            if item == 1:
                raise ValueError("item one failed")
            return item

        outcomes = map_in_order(call, [0, 1, 2], catch=(ValueError,))
        self.assertEqual([(0, None), (2, None)], [outcomes[0], outcomes[2]])
        self.assertEqual((None, "item one failed"), (outcomes[1][0], str(outcomes[1][1])))

    def test_fatal_and_uncaught_errors_stop_the_rest(self) -> None:
        for catch, fatal in (((ValueError,), lambda error: True), ((), lambda error: False)):
            started: list[int] = []
            release = threading.Event()

            def call(item: int, *, started: list[int] = started, release: threading.Event = release) -> int:
                started.append(item)
                if item == 1:
                    raise ValueError("stop")
                release.wait(timeout=0.5)
                return item

            with self.subTest(catch=catch), self.assertRaisesRegex(ValueError, "stop"):
                map_in_order(call, range(12), catch=catch, fatal=fatal)
            self.assertLess(len(started), 12, "calls not yet started are cancelled")

    def test_one_item_runs_in_the_calling_thread(self) -> None:
        caller = threading.get_ident()
        self.assertEqual([(caller, None)], map_in_order(lambda item: threading.get_ident(), ["only"]))
        self.assertEqual([(caller, None)] * 2, map_in_order(lambda item: threading.get_ident(), "ab", workers=1))


class StateAndLockTests(unittest.TestCase):
    def test_state_update_checks_expected_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            initial = empty_state()

            def advance(state: dict) -> dict:
                replacement = copy.deepcopy(state)
                replacement["repositories"]["example/one"] = {
                    "merged_since": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                }
                return replacement

            updated = update_state(path, advance, expected=initial)
            self.assertEqual(updated, load_state(path))
            with self.assertRaisesRegex(StateError, "changed"):
                update_state(path, advance, expected=initial)

    def test_contended_lock_fails_without_removing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            with ResourceLock(path):
                with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.01):
                    pass
                self.assertTrue(path.exists())

    def _old_lock(self, path: Path, owner: dict) -> None:
        path.mkdir()
        atomic_write_json(
            path / "owner.json",
            {"token": "a" * 32, "created_unix": time.time() - 7200, **owner},
        )

    def test_lock_records_its_holder_start_time(self) -> None:
        probed: list[int] = []

        def probe(pid: int) -> ProcessStatus:
            probed.append(pid)
            return ProcessStatus(True, 1234)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            with ResourceLock(path, probe=probe):
                owner = read_json(path / "owner.json")
            self.assertEqual(os.getpid(), owner["pid"])
            self.assertEqual(1234, owner["start_time"])
            self.assertEqual([os.getpid()], probed)

    def test_old_lock_of_live_owner_is_not_reclaimed(self) -> None:
        cases = {
            "same process": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, 1234)),
            "no recorded start time": ({"pid": 4242}, ProcessStatus(True, 1234)),
            "recorded start time not an integer": ({"pid": 4242, "start_time": True}, ProcessStatus(True, 1)),
            "start time unreadable": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, None)),
            "pid not positive": ({"pid": 0, "start_time": 1234}, ProcessStatus(False, None)),
        }
        for name, (owner, status) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                self._old_lock(path, owner)
                with (
                    self.assertRaisesRegex(PersistenceError, "Timed out"),
                    ResourceLock(path, timeout_seconds=0.01, probe=lambda pid, status=status: status),
                ):
                    pass
                self.assertEqual(owner["pid"], read_json(path / "owner.json")["pid"])

    def test_old_lock_of_dead_or_reused_owner_is_reclaimed(self) -> None:
        cases = {
            "dead": ({"pid": 4242, "start_time": 1234}, ProcessStatus(False, None)),
            "dead without recorded start time": ({"pid": 4242}, ProcessStatus(False, None)),
            "pid reused": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, 5678)),
        }
        for name, (owner, status) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                self._old_lock(path, owner)

                def probe(pid: int, *, status: ProcessStatus = status) -> ProcessStatus:
                    return ProcessStatus(True, 9999) if pid == os.getpid() else status

                with ResourceLock(path, timeout_seconds=0.5, probe=probe):
                    self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
                self.assertFalse(path.exists())

    def test_recent_lock_is_not_probed(self) -> None:
        def probe(pid: int) -> ProcessStatus:
            if pid != os.getpid():
                raise AssertionError(f"probed PID {pid}")
            return ProcessStatus(True, 1)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            atomic_write_json(
                path / "owner.json",
                {"pid": 4242, "token": "a" * 32, "created_unix": time.time(), "start_time": 1},
            )
            with (
                self.assertRaisesRegex(PersistenceError, "Timed out"),
                ResourceLock(path, timeout_seconds=0.01, probe=probe),
            ):
                pass

    def test_lock_probes_identity_by_default_and_never_signals(self) -> None:
        probed: list[int] = []

        def probe(pid: int) -> ProcessStatus:
            probed.append(pid)
            return ProcessStatus(pid == os.getpid(), 1234 if pid == os.getpid() else None)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            self._old_lock(path, {"pid": 4242, "start_time": 1234})
            with (
                mock.patch.object(review_io, "process_status", probe),
                mock.patch("os.kill", side_effect=AssertionError("os.kill called")),
                ResourceLock(path, timeout_seconds=0.5),
            ):
                self.assertEqual(1234, read_json(path / "owner.json")["start_time"])
            self.assertIn(4242, probed)

    def test_atomic_json_is_restrictive_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"answer": 42})
            self.assertEqual({"answer": 42}, read_json(path))
            if os.name != "nt":
                self.assertEqual(0o600, path.stat().st_mode & 0o777)


class RecordTests(unittest.TestCase):
    def test_adapter_schema_matches_runtime_finding_contract(self) -> None:
        schema = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json").read_text(encoding="utf-8")
        )
        finding = schema["properties"]["findings"]["items"]
        self.assertFalse(finding["additionalProperties"])
        self.assertEqual(
            {
                "candidate_key",
                "severity",
                "category",
                "path",
                "line",
                "body",
                "evidence",
                "source",
            },
            set(finding["required"]),
        )
        self.assertEqual(
            {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"},
            set(finding["properties"]["severity"]["enum"]),
        )
        title = finding["properties"]["title"]
        self.assertEqual((1, 120), (title["minLength"], title["maxLength"]))
        self.assertEqual(120, TITLE_MAXIMUM_LENGTH)
        for value, accepted in (("Headline", True), ("a b", True), (" padded", False), ("two\nlines", False)):
            self.assertEqual(accepted, re.fullmatch(title["pattern"], value) is not None, value)
            self.assertEqual(accepted, valid_title(value), value)
        self.assertFalse(valid_title("x" * 121))
        self.assertFalse(valid_title(None))

    def test_finding_title_is_optional_but_validated_in_results_and_records(self) -> None:
        def validate(result: dict) -> dict:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        untitled = validate(valid_adapter_result())
        self.assertNotIn("title", build_record(valid_request(), untitled, version=1, policy={})["findings"][0])
        titled = valid_adapter_result()
        titled["findings"][0]["title"] = "Zero is not handled"
        record = build_record(valid_request(), validate(titled), version=1, policy={})
        self.assertEqual("Zero is not handled", validate_record(record)["findings"][0]["title"])
        for bad in ("", "x" * 121, "two\nlines", 7):
            result = valid_adapter_result()
            result["findings"][0]["title"] = bad
            with self.assertRaisesRegex(RecordError, "title must be a single non-blank line"):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["title"] = bad
            with self.assertRaisesRegex(RecordError, "F001.title must be a single non-blank line"):
                validate_record(tampered)

    def test_finding_analyzer_is_optional_but_validated_in_results_records_and_schema(self) -> None:
        schema = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json").read_text(encoding="utf-8")
        )
        analyzer = schema["properties"]["findings"]["items"]["properties"]["analyzer"]
        self.assertEqual(["available", "known", "custom-candidate"], analyzer["properties"]["coverage"]["enum"])
        self.assertEqual({"coverage", "tool", "rule"}, set(analyzer["required"]))
        self.assertFalse(analyzer["additionalProperties"])
        name_pattern = analyzer["properties"]["tool"]["pattern"]
        self.assertEqual(name_pattern, analyzer["properties"]["rule"]["pattern"])
        custom_rule = analyzer["then"]["properties"]["rule"]
        self.assertEqual(60, custom_rule["maxLength"])
        for coverage, tool, rule, accepted in (
            ("available", "StyleCop.Analyzers", "SA1515", True),
            ("known", "@typescript-eslint/eslint-plugin", "no-floating-promises", True),
            ("custom-candidate", "Roslyn", "unbounded-retry-loop", True),
            ("custom-candidate", "Roslyn", "x" * 60, True),
            ("custom-candidate", "Roslyn", "x" * 61, False),
            ("custom-candidate", "Roslyn", "CA2000", False),
            ("custom-candidate", "Roslyn", "trailing-", False),
            ("known", "Roslynator Analyzers", "RCS1001", False),
            ("known", "Roslynator.Analyzers", "RCS|1001", False),
            ("known", "Roslynator.Analyzers", "x" * 101, False),
            ("known", "<b>", "RCS1001", False),
            ("known", "", "RCS1001", False),
            ("maybe", "Roslynator.Analyzers", "RCS1001", False),
        ):
            value = {"coverage": coverage, "tool": tool, "rule": rule}
            self.assertEqual(accepted, valid_analyzer(value), value)
            by_schema = (
                coverage in analyzer["properties"]["coverage"]["enum"]
                and all(re.fullmatch(name_pattern, text) for text in (tool, rule))
                and (
                    coverage != "custom-candidate"
                    or (len(rule) <= custom_rule["maxLength"] and re.fullmatch(custom_rule["pattern"], rule))
                )
            )
            self.assertEqual(accepted, bool(by_schema), value)
        self.assertFalse(valid_analyzer({"coverage": "known", "tool": "x", "rule": "y", "extra": 1}))
        self.assertFalse(valid_analyzer({"coverage": ["known"], "tool": "x", "rule": "y"}))
        self.assertFalse(valid_analyzer("known"))

        def validate(result: dict) -> dict:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        self.assertNotIn(
            "analyzer",
            build_record(valid_request(), validate(valid_adapter_result()), version=1, policy={})["findings"][0],
        )
        covered = valid_adapter_result()
        coverage = {"coverage": "available", "tool": "Microsoft.CodeAnalysis.NetAnalyzers", "rule": "CA2000"}
        covered["findings"][0]["analyzer"] = coverage
        record = build_record(valid_request(), validate(covered), version=1, policy={})
        self.assertEqual(coverage, validate_record(record)["findings"][0]["analyzer"])
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn(
            "> **Analyzer:** `CA2000` in `Microsoft.CodeAnalysis.NetAnalyzers`, which the repository "
            "already has, would catch this if enforced.\n",
            markdown,
        )
        for bad in (
            {"coverage": "known", "tool": "x"},
            {"coverage": "custom-candidate", "tool": "Roslyn", "rule": "A B"},
        ):
            result = valid_adapter_result()
            result["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(RecordError, "analyzer must be an object with exactly coverage"):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(RecordError, "F001.analyzer must be an object with exactly coverage"):
                validate_record(tampered)
        unknown = valid_adapter_result()
        unknown["findings"][0]["note"] = "x"
        with self.assertRaisesRegex(RecordError, "Adapter finding fields do not match the protocol"):
            validate(unknown)

    def test_published_adapter_fixture_satisfies_protocol(self) -> None:
        result = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "fixtures" / "adapter-result.json").read_text(encoding="utf-8")
        )
        validate_adapter_result(
            result,
            expected_repository="example/example-repository",
            expected_number=17,
            expected_head_sha="b" * 40,
        )

    def test_adapter_result_requires_every_prior_disposition(self) -> None:
        result = valid_adapter_result()
        with self.assertRaisesRegex(RecordError, "missing"):
            validate_adapter_result(
                result,
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
                prior_ids=["F001"],
            )

    def test_comment_dispositions_are_required_except_from_older_repository_reviewers(self) -> None:
        arguments = {
            "expected_repository": "example/one",
            "expected_number": 12,
            "expected_head_sha": "b" * 40,
            "comment_ids": ["C1", "C2"],
        }
        with self.assertRaisesRegex(RecordError, r"Comment dispositions mismatch; missing=\['C1', 'C2'\]"):
            validate_adapter_result(valid_adapter_result(), **arguments)
        # A repository entrypoint reviewer that predates comment dispositions may omit them all...
        validate_adapter_result(valid_adapter_result(), require_comment_dispositions=False, **arguments)
        # ...but one that gives any must cover every comment exactly once.
        partial = {
            **valid_adapter_result(),
            "comment_dispositions": [{"comment_id": "C1", "disposition": "addressed", "rationale": "Done."}],
        }
        with self.assertRaisesRegex(RecordError, r"missing=\['C2'\]"):
            validate_adapter_result(partial, require_comment_dispositions=False, **arguments)

    def test_record_patches_and_re_review_scope_are_validated(self) -> None:
        patches = {"src/file.cs": {"sha256": "c" * 64, "lines": 3}}
        scope = {
            "requested": "auto",
            "used": "incremental",
            "reason": "a small change",
            "since_version": 1,
            "files_changed": 1,
            "files_total": 1,
            "lines_changed": 3,
            "lines_total": 3,
        }
        request = {**valid_request(), "mode": "re-review", "patches": patches, "scope": scope}
        record = build_record(request, valid_adapter_result(), version=2, policy={})
        validate_record(record)
        self.assertEqual((patches, scope), (record["review"]["patches"], record["review"]["scope"]))
        self.assertIn(
            "| **Scope** | incremental, 1 of 1 files and 3 of 3 changed lines differ from v1 (requested "
            "auto: a small change) |\n",
            render_markdown(record, record_payload_hash="0" * 64),
        )
        uncompared = copy.deepcopy(record)
        uncompared["review"]["scope"].update(used="full", files_changed=None, lines_changed=None)
        validate_record(uncompared)
        self.assertIn("full, could not compare with v1", render_markdown(uncompared, record_payload_hash="0" * 64))
        for mutate, message in (
            (lambda r: r["review"].update(patches={}), "patches must be a non-empty object"),
            (lambda r: r["review"].update(patches={"../x.cs": patches["src/file.cs"]}), "patch path is unsafe"),
            (lambda r: r["review"]["patches"]["src/file.cs"].update(sha256="C" * 64), "patch for src/file.cs"),
            (lambda r: r["review"]["patches"]["src/file.cs"].update(lines=-1), "patch for src/file.cs"),
            (lambda r: r["review"].update(mode="initial"), "Only a re-review has a scope"),
            (lambda r: r["review"]["scope"].update(requested="most"), "scope is invalid"),
            (lambda r: r["review"]["scope"].update(used="auto"), "scope is invalid"),
            (lambda r: r["review"]["scope"].update(reason=" "), "needs a reason"),
            (lambda r: r["review"]["scope"].update(since_version=2), "since_version must be an earlier version"),
            (lambda r: r["review"]["scope"].update(since_version=0), "since_version must be an earlier version"),
            (lambda r: r["review"]["scope"].update(files_total=-1), "totals must be non-negative"),
            (lambda r: r["review"]["scope"].update(files_changed=2), "within their totals"),
            (lambda r: r["review"]["scope"].update(lines_changed=None), "both be null"),
            (
                lambda r: r["review"]["scope"].update(files_changed=None, lines_changed=None),
                "incremental re-review must have compared",
            ),
            (lambda r: r["review"]["scope"].pop("reason"), "scope fields are malformed"),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)
        without = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertFalse({"patches", "scope"} & set(without["review"]), "older records have neither")

    def test_record_comments_and_reviewers_are_validated(self) -> None:
        request = {
            **valid_request(),
            "reviewers": [
                {
                    "id": "generic-review",
                    "category": "General",
                    "files": 2,
                    "findings": 1,
                    "retries": 0,
                    "dispositions_only": False,
                }
            ],
            "github_comments": [
                {
                    "id": "C1",
                    "author": "dev",
                    "path": "src/file.cs",
                    "line": None,
                    "outdated": False,
                    "body": "Why?",
                    "url": "https://example.invalid/1",
                }
            ],
        }
        result = {
            **valid_adapter_result(),
            "comment_dispositions": [{"comment_id": "C1", "disposition": "still_present", "rationale": "Unchanged."}],
        }
        record = build_record(request, result, version=1, policy={"request_changes_for": ["MUST_FIX"]})
        validate_record(record)
        for mutate, message in (
            (lambda r: r.pop("comment_dispositions"), "comment_dispositions must be an array"),
            (lambda r: r.pop("github_comments"), "need the comments"),
            (lambda r: r["comment_dispositions"].clear(), r"missing=\['C1'\]"),
            (lambda r: r["github_comments"][0].update(id="X1"), "C<n>"),
            (lambda r: r["review"]["reviewers"][0].update(retries=-1), "non-negative"),
            (lambda r: r["review"]["reviewers"].append(dict(r["review"]["reviewers"][0])), "unique"),
            (lambda r: r["review"].update(reviewers=[]), "non-empty"),
            (lambda r: r["review"]["reviewers"][0].update(seconds=-1), "seconds must be a non-negative integer"),
            (lambda r: r["review"]["reviewers"][0].update(seconds=1.5), "seconds must be a non-negative integer"),
            (lambda r: r["review"]["reviewers"][0].update(seconds=True), "seconds must be a non-negative integer"),
            (lambda r: r["review"]["reviewers"][0].update(minutes=1), "fields are malformed"),
            (lambda r: r["review"]["reviewers"][0].update(model=""), "model must be a single non-blank line"),
            (lambda r: r["review"]["reviewers"][0].update(model="a\nb"), "model must be a single non-blank line"),
            (lambda r: r["review"]["reviewers"][0].update(model=5), "model must be a single non-blank line"),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)
        timed = copy.deepcopy(record)
        timed["review"]["reviewers"][0].update(seconds=0, model="claude-sonnet-5")
        validate_record(timed)  # a reviewer's time is optional and may be zero
        without = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertNotIn("github_comments", without)
        self.assertNotIn("reviewers", without["review"])

        result["prior_dispositions"] = [{"finding_id": "v1:F001", "disposition": "addressed", "rationale": "Fixed."}]
        first = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        markdown = render_markdown(
            build_record({**request, "mode": "re-review"}, result, version=2, policy={}, prior_ledger=first["ledger"]),
            record_payload_hash="0" * 64,
            prior_records=[first],
        )
        # Prior findings are shown with the findings; review comments keep a section of their own.
        self.assertLess(markdown.index("Addressed in v2: Fixed."), markdown.index("## Review Comments"))
        self.assertIn(
            "| [C1](https://example.invalid/1) | @dev on `src/file.cs`: Why? | STILL PRESENT | Unchanged. |", markdown
        )
        self.assertIn("## Reviewers", markdown)
        self.assertLess(markdown.index("## Review Comments"), markdown.index("## Reviewers"))
        self.assertLess(markdown.index("## Reviewers"), markdown.index("Review Details</strong>"))
        self.assertIn("| STILL PRESENT | Unchanged. |\n\n**0/1 addressed**\n", markdown)
        self.assertEqual(1, markdown.count(" addressed**"), "the prior findings' count is in the verdict row")

    def test_unhashable_values_are_validation_errors_not_crashes(self) -> None:
        arguments = {"expected_repository": "example/one", "expected_number": 12, "expected_head_sha": "b" * 40}
        for mutate, message in (
            (lambda r: r.update(status=["complete"]), "status is invalid"),
            (lambda r: r["findings"][0].update(severity=["MUST_FIX"]), "Invalid finding severity"),
            (
                lambda r: r.update(
                    prior_dispositions=[{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
                ),
                "Invalid disposition",
            ),
            (
                lambda r: r.update(
                    comment_dispositions=[{"comment_id": ["C1"], "disposition": "addressed", "rationale": "x"}]
                ),
                "unique strings",
            ),
        ):
            result = valid_adapter_result()
            result["prior_dispositions"] = [{"finding_id": "F001", "disposition": "addressed", "rationale": "x"}]
            mutate(result)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_adapter_result(result, prior_ids=["F001"], comment_ids=["C1"], **arguments)
        record = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        for field, value in (("verdict", ["APPROVED"]), ("mode", {"initial": 1})):
            broken = copy.deepcopy(record)
            broken["review"][field] = value
            with self.subTest(field=field), self.assertRaises(RecordError):
                validate_record(broken)
        broken = copy.deepcopy(record)
        broken["prior_dispositions"] = [{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
        with self.assertRaisesRegex(RecordError, "prior disposition is invalid"):
            validate_record(broken)

    def test_adapter_result_rejects_unsafe_paths_and_mismatched_sha(self) -> None:
        result = valid_adapter_result()
        result["findings"][0]["path"] = "../outside.cs"
        with self.assertRaisesRegex(RecordError, "safe repository-relative"):
            validate_adapter_result(
                result,
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            )

    def test_record_validation_rejects_count_tampering(self) -> None:
        result = validate_adapter_result(
            valid_adapter_result(),
            expected_repository="example/one",
            expected_number=12,
            expected_head_sha="b" * 40,
        )
        record = build_record(
            valid_request(),
            result,
            version=1,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        )
        record["review"]["counts"]["SHOULD_FIX"] = 0
        with self.assertRaisesRegex(RecordError, "counts"):
            write_record_pair(Path("unused.json"), Path("unused.md"), record)
        result = valid_adapter_result()
        result["head_sha"] = "c" * 40
        with self.assertRaisesRegex(RecordError, "head SHA"):
            validate_adapter_result(
                result,
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            )

    def test_verdict_policy_is_centralized(self) -> None:
        ledger = [{"severity": "SHOULD_FIX", "state": "open"}] * 2
        policy = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
        self.assertEqual("APPROVED", calculate_verdict(ledger, policy))
        ledger.append({"severity": "SHOULD_FIX", "state": "open"})
        self.assertEqual("CHANGES_REQUESTED", calculate_verdict(ledger, policy))

    def test_record_pair_detects_markdown_tampering(self) -> None:
        request = valid_request()
        result = validate_adapter_result(
            valid_adapter_result(),
            expected_repository="example/one",
            expected_number=12,
            expected_head_sha="b" * 40,
        )
        record = build_record(
            request,
            result,
            version=1,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            reviewed_at="2026-01-01T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path = root / "review.json"
            markdown_path = root / "review.md"
            write_record_pair(json_path, markdown_path, record)
            validated = validate_record_pair(json_path, markdown_path)
            self.assertEqual("F001", validated["findings"][0]["id"])
            markdown_path.write_text(markdown_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RecordError, "Markdown hash mismatch"):
                validate_record_pair(json_path, markdown_path)

    def test_markdown_groups_findings_by_severity_in_the_legacy_layout(self) -> None:
        request = valid_request()
        request["mode"] = "re-review"
        request["title"] = "Fix | split"
        result = valid_adapter_result()
        result["findings"] = [
            {
                **result["findings"][0],
                "candidate_key": "late",
                "severity": "SUGGESTION",
                "path": "src/z.cs",
                "line": 9,
                "body": "Prefer a constant.",
                "evidence": "z.cs:9 adds: x",
            },
            {
                **result["findings"][0],
                "candidate_key": "early",
                "severity": "MUST_FIX",
                "category": "A<B>",
                "path": "src/a.cs",
                "line": 3,
                "title": "List<T> & friends leak",
                "body": "First paragraph.\n\nSecond paragraph.",
                "evidence": "a.cs:3 adds: f(`x`) | y",
            },
        ]
        result["prior_dispositions"] = [
            {"finding_id": "v2:F001", "disposition": "partially_addressed", "rationale": "One | of two."}
        ]
        prior = build_record(valid_request(), valid_adapter_result(), version=2, policy={})
        record = build_record(
            request, result, version=3, policy={}, reviewed_at="2026-10-01T23:26:42+02:00", prior_ledger=prior["ledger"]
        )
        markdown = render_markdown(record, record_payload_hash="0" * 64, prior_records=[prior])
        self.assertTrue(markdown.startswith("# Code Review — example/one#12 (re-review v3)\n\n| | |\n|---|---|\n"))
        for expected in (
            "| **Title** | Fix \\| split |",
            "| **Reviewed** | 01-Oct-2026 21:26 UTC |",
            "| **Verdict** | CHANGES REQUESTED, 3 open since v2 |",
            "<summary><strong>MUST FIX (1)</strong></summary>",
            "<summary>v3 F001. [A&lt;B&gt;] List&lt;T&gt; &amp; friends leak</summary>",
            "<summary>v3 F002. [Correctness] <code>z.cs:9</code></summary>",
            "> **File:** `src/a.cs`  \n> **Line:** 3 | **Source:** generic\n>\n"
            "> First paragraph.\n>\n> Second paragraph.\n>\n> **Evidence:** ``a.cs:3 adds: f(`x`) | y``\n",
            "<summary><strong>SHOULD FIX (1)</strong></summary>",
            "> **Open since v2.** Partially addressed in v3: One | of two.\n",
            "<summary><strong>SUGGESTIONS (1)</strong></summary>",
            "| **Record payload SHA-256** | `" + "0" * 64 + "` |",
        ):
            self.assertIn(expected, markdown)
        self.assertLess(markdown.index("MUST FIX (1)"), markdown.index("SHOULD FIX (1)"))
        self.assertLess(markdown.index("SHOULD FIX (1)"), markdown.index("SUGGESTIONS (1)"))
        self.assertTrue(markdown.endswith(f"<!-- reviewed_head_sha: {'b' * 40} -->\n"))
        self.assertEqual(len(re.findall(r"<details[ >]", markdown)), markdown.count("</details>"))
        self.assertNotIn("addressed**", markdown)
        self.assertIn("<details>\n<summary><strong>Review Details</strong></summary>", markdown)
        self.assertIn("| **Base** | `main` |", markdown)
        empty = valid_adapter_result()
        empty["findings"] = []
        markdown = render_markdown(
            build_record(valid_request(), empty, version=1, policy={}), record_payload_hash="0" * 64
        )
        self.assertIn("## Findings\n\nNo findings.\n", markdown)
        self.assertNotIn("<details open>", markdown)
        self.assertNotIn("Prior Findings Status", markdown)

    def test_specialist_evidence_joins_the_line_and_the_head_branch_is_shown(self) -> None:
        request = {**valid_request(), "head_ref": "feature/zero-total"}
        result = valid_adapter_result()
        result["findings"][0]["evidence"] = "src/file.cs:42 adds:     return items.Sum();"
        record = build_record(request, result, version=1, policy={})
        validate_record(record)
        self.assertEqual("feature/zero-total", record["pull_request"]["head_ref"])
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("| **Branch** | `feature/zero-total` → `main` |", markdown)
        self.assertIn("> **Line 42:** `return items.Sum();` | **Source:** generic\n", markdown)
        self.assertNotIn("**Evidence:**", markdown)
        broken = copy.deepcopy(record)
        broken["pull_request"]["head_ref"] = " "
        with self.assertRaisesRegex(RecordError, "head_ref is invalid"):
            validate_record(broken)


class ArchiveTests(unittest.TestCase):
    def _record(self, repository: str, number: int, version: int) -> dict:
        request = valid_request(repository, number)
        result = validate_adapter_result(
            valid_adapter_result(repository, number),
            expected_repository=repository,
            expected_number=number,
            expected_head_sha="b" * 40,
        )
        return build_record(
            request,
            result,
            version=version,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        )

    def test_owner_prevents_same_short_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = pull_directory(root, "owner-a/shared", 1)
            second = pull_directory(root, "owner-b/shared", 1)
            self.assertNotEqual(first, second)

    def test_versions_are_allocated_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit_record(
                root,
                "example/one",
                12,
                self._record("example/one", 12, 1),
                expected_latest_version=None,
            )
            with self.assertRaisesRegex(ArchiveError, "changed concurrently"):
                commit_record(
                    root,
                    "example/one",
                    12,
                    self._record("example/one", 12, 2),
                    expected_latest_version=None,
                )
            commit_record(
                root,
                "example/one",
                12,
                self._record("example/one", 12, 2),
                expected_latest_version=1,
            )
            self.assertEqual(2, latest_record(root, "example/one", 12)["review"]["version"])

    def test_versions_list_every_number_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            names = ["review.json", *(f"review-v{n}.json" for n in range(2, 12)), "review-v100.json"]
            # Not record names: version 1 is review.json, and versions carry no leading zeros.
            names += ["review-v1.json", "review-v0.json", "review-v01.json", "review-v010.json", "review-vx.json"]
            for name in names:
                (directory / name).write_text("{}", encoding="utf-8")
            self.assertEqual([*range(1, 12), 100], list_versions(directory))

    def test_eleventh_review_follows_the_tenth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in range(1, 12):
                json_path, _, _ = commit_record(
                    root,
                    "example/one",
                    12,
                    self._record("example/one", 12, version),
                    expected_latest_version=version - 1 if version > 1 else None,
                )
            self.assertEqual("review-v11.json", json_path.name)
            self.assertEqual(11, latest_record(root, "example/one", 12)["review"]["version"])


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
            with self.assertRaises(FlagError):
                resolve_flag(path, created["id"], "Again")

    def test_a_finding_is_named_with_its_review_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flags.json"
            for options in (
                {"repository": "example/one", "pull_number": 12, "finding_id": "F001"},
                {"repository": "example/one", "review_version": 1, "finding_id": "F001"},
                {"repository": "example/one", "pull_number": 12, "review_version": 0, "finding_id": "F001"},
                {"repository": "example/one", "review_version": 1},
                {"repository": "example/one", "pull_number": 12, "review_version": True},
            ):
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


class GitHubTests(unittest.TestCase):
    @staticmethod
    def _api_pull(number: int, *, state: str = "open", merged_at: str | None = None) -> dict:
        return {
            "number": number,
            "title": f"Pull {number}",
            "html_url": f"https://github.com/example/one/pull/{number}",
            "state": state,
            "draft": False,
            "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": f"pull-{number}", "sha": "b" * 40},
            "merged_at": merged_at,
        }

    def test_missing_cli_fails_with_prerequisite_error(self) -> None:
        with (
            mock.patch(
                "review_github.subprocess.run",
                side_effect=FileNotFoundError("gh was not found"),
            ),
            self.assertRaises(GitHubError) as context,
        ):
            review_github.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertIn("install GitHub CLI", str(context.exception))

    def test_pagination_is_flattened(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(
                0,
                json.dumps([[self._api_pull(1)], [self._api_pull(2)]]),
                "",
            )

        pulls = GitHubClient(runner).list_pulls("example/one", state="open")
        self.assertEqual([1, 2], [item["number"] for item in pulls])
        self.assertIn("--paginate", calls[0])
        self.assertIn("--slurp", calls[0])

    @staticmethod
    def _graphql_page(numbers: list[int], cursor: str | None) -> str:
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                            "nodes": [{"number": number} for number in numbers],
                        }
                    }
                }
            }
        )

    def test_graphql_connection_is_paginated_until_complete(self) -> None:
        calls: list[list[str]] = []
        pages = {None: self._graphql_page([1, 2], "c1"), "c1": self._graphql_page([3], None)}

        def runner(arguments: list[str]) -> CommandResult:
            calls.append(list(arguments))
            after = next((value.removeprefix("after=") for value in arguments if value.startswith("after=")), None)
            return CommandResult(0, pages[after], "")

        nodes = GitHubClient(runner).graphql_nodes(
            "query($after: String) { x }", {"owner": "example", "name": "one"}, ("repository", "pullRequests")
        )
        self.assertEqual([1, 2, 3], [node["number"] for node in nodes])
        self.assertEqual(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=query($after: String) { x }",
                "-f",
                "owner=example",
                "-f",
                "name=one",
            ],
            calls[0],
        )
        self.assertEqual(["-f", "after=c1"], calls[1][-2:])

    def test_graphql_failures_never_return_partial_nodes(self) -> None:
        connection = ("repository", "pullRequests")
        cases = (
            (
                CommandResult(0, json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}), ""),
                "rate_limit",
                "slow down",
            ),
            (CommandResult(0, json.dumps({"data": {"repository": None}}), ""), "not_found", "was not found"),
            (
                CommandResult(0, json.dumps({"data": {"repository": {"pullRequests": {"nodes": []}}}}), ""),
                "malformed",
                "unexpected shape",
            ),
            (CommandResult(0, self._graphql_page([1], "same"), ""), "malformed", "did not advance"),
            (CommandResult(1, "", "HTTP 401: Bad credentials"), "authentication", "Bad credentials"),
            (CommandResult(0, "not-json", ""), "malformed", "malformed JSON"),
        )
        for response, kind, message in cases:
            with self.subTest(kind=kind, message=message):
                client = GitHubClient(lambda arguments, result=response: result)
                with self.assertRaisesRegex(GitHubError, message) as context:
                    client.graphql_nodes("query", {}, connection)
                self.assertEqual(kind, context.exception.kind)

    def test_authenticated_login_is_shape_checked(self) -> None:
        self.assertEqual(
            "octo",
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({"login": "octo"}), "")).authenticated_login(),
        )
        with self.assertRaisesRegex(GitHubError, "unexpected shape"):
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({}), "")).authenticated_login()

    def test_single_pull_fetch_is_explicit_and_shape_checked(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(0, json.dumps(self._api_pull(42)), "")

        pull = GitHubClient(runner).get_pull("Example/One", 42)
        self.assertEqual(42, pull["number"])
        self.assertEqual("repos/example/one/pulls/42", calls[0][-1])
        self.assertNotIn("--paginate", calls[0])
        with self.assertRaisesRegex(GitHubError, "positive"):
            GitHubClient(runner).get_pull("example/one", 0)
        malformed = GitHubClient(lambda arguments: CommandResult(0, json.dumps([]), ""))
        with self.assertRaisesRegex(GitHubError, "unexpected shape"):
            malformed.get_pull("example/one", 42)

    def test_closed_unmerged_pulls_are_ineligible(self) -> None:
        closed = self._api_pull(42, state="closed")
        client = GitHubClient(lambda arguments: CommandResult(0, json.dumps(closed), ""))
        with self.assertRaisesRegex(GitHubError, "closed without being merged"):
            client.get_pull("example/one", 42)

    def test_api_errors_fail_closed_and_expected_404_can_be_absent(self) -> None:
        def runner(arguments: list[str]) -> CommandResult:
            return CommandResult(1, "", "HTTP 404: Not Found")

        client = GitHubClient(runner)
        with self.assertRaises(GitHubError) as context:
            client.api_json("repos/example/one")
        self.assertEqual("not_found", context.exception.kind)
        self.assertIsNone(client.api_json("repos/example/one", allow_absent=True))

    def test_malformed_json_fails(self) -> None:
        client = GitHubClient(lambda arguments: CommandResult(0, "not-json", ""))
        with self.assertRaises(GitHubError) as context:
            client.api_json("repos/example/one")
        self.assertEqual("malformed", context.exception.kind)

    def test_api_failure_kinds_are_classified(self) -> None:
        cases = (
            ("API rate limit exceeded", "rate_limit"),
            ("HTTP 429: too many requests", "rate_limit"),
            ("HTTP 401: authentication required", "authentication"),
            ("not logged into GitHub", "authentication"),
            ("HTTP 403: forbidden", "forbidden"),
            ("HTTP 404: not found", "not_found"),
            ("connection reset", "api"),
        )
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                client = GitHubClient(lambda arguments, message=stderr: CommandResult(1, "", message))
                with self.assertRaises(GitHubError) as context:
                    client.api_json("repos/example/one")
                self.assertEqual(expected, context.exception.kind)

    def test_runner_decodes_bytes_that_are_not_utf8_without_losing_them(self) -> None:
        result = review_github.subprocess_runner(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'caf\\xe9'); sys.stderr.buffer.write(b'bad \\xff')",
            ]
        )
        self.assertEqual((0, "caf\udce9", "bad \udcff"), (result.returncode, result.stdout, result.stderr))

    def test_the_diff_replaces_and_counts_each_undecodable_byte(self) -> None:
        def undecodable(data: bytes) -> str:
            return data.decode("utf-8", "surrogateescape")  # what subprocess_runner returns for these bytes

        cases = (
            (b"+caf\xe9\n", "+caf\ufffd\n", 1),
            (b"+\xe2\x82 cut short\n", "+\ufffd\ufffd cut short\n", 2),  # a truncated sequence is two bytes
            ("+caf\ufffd already\n".encode("utf-8"), "+caf\ufffd already\n", 0),  # a real U+FFFD is not counted
            (b"+plain\n", "+plain\n", 0),
        )
        for data, text, count in cases:
            with self.subTest(data=data):
                value = undecodable(data)
                client = GitHubClient(lambda arguments, value=value: CommandResult(0, value, ""))
                self.assertEqual((text, count), client.get_pull_diff("example/one", 7))

    def test_json_and_errors_replace_undecodable_bytes(self) -> None:
        body = b'{"login": "caf\xe9"}'.decode("utf-8", "surrogateescape")
        self.assertEqual("caf\ufffd", GitHubClient(lambda arguments: CommandResult(0, body, "")).authenticated_login())
        page = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"body": "BODY"}],
                        }
                    }
                }
            }
        )
        page = page.replace("BODY", b"caf\xe9".decode("utf-8", "surrogateescape"))
        nodes = GitHubClient(lambda arguments: CommandResult(0, page, "")).graphql_nodes(
            "query", {}, ("repository", "pullRequests")
        )
        self.assertEqual([{"body": "caf\ufffd"}], nodes)
        failing = GitHubClient(
            lambda arguments: CommandResult(1, "", b"HTTP 404: caf\xe9 not found".decode("utf-8", "surrogateescape"))
        )
        with self.assertRaises(GitHubError) as context:
            failing.get_pull_diff("example/one", 7)
        # The message reaches a strict UTF-8 stderr as a FAILED line, so it must encode.
        self.assertEqual("HTTP 404: caf\ufffd not found", str(context.exception))
        self.assertEqual("not_found", context.exception.kind)
        str(context.exception).encode("utf-8")


class RuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _manifest() -> dict:
        return {
            "schema_version": 1,
            "id": "fixture-review",
            "protocol_version": 1,
            "supports": ["initial", "re-review"],
            "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
            "entrypoint": ".claude/skills/fixture-review/SKILL.md",
            "resources": [".claude/skills/fixture-review/references/rules.md"],
            "agent_profiles": [".claude/agents/fixture-review.md"],
        }

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def _repository(self, root: Path) -> tuple[Path, str, str]:
        checkout = root / "checkout"
        checkout.mkdir()
        self._git(checkout, "init", "-b", "main")
        self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
        manifest_path = checkout / ".review" / "adapter.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
        for relative, content in {
            ".claude/skills/fixture-review/SKILL.md": "# Trusted fixture\n",
            ".claude/skills/fixture-review/references/rules.md": "Rules\n",
            ".claude/agents/fixture-review.md": "Agent\n",
        }.items():
            target = checkout / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._git(checkout, "add", ".")
        self._git(
            checkout,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "trusted",
        )
        trusted = self._git(checkout, "rev-parse", "HEAD")
        (checkout / "head.txt").write_text("untrusted change", encoding="utf-8")
        self._git(checkout, "add", "head.txt")
        self._git(
            checkout,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "head",
        )
        head = self._git(checkout, "rev-parse", "HEAD")
        return checkout, trusted, head

    def test_manifest_with_unhashable_supports_is_rejected_not_a_crash(self) -> None:
        manifest = self._manifest()
        manifest["supports"] = [["initial"]]
        with self.assertRaisesRegex(RuntimeContractError, "supported modes"):
            validate_adapter_manifest(manifest)

    def test_manifest_rejects_unsafe_or_duplicate_files(self) -> None:
        manifest = self._manifest()
        manifest["resources"] = ["../outside.md"]
        with self.assertRaisesRegex(RuntimeContractError, "unsafe"):
            validate_adapter_manifest(manifest)
        manifest = self._manifest()
        manifest["resources"] = [manifest["entrypoint"]]
        with self.assertRaisesRegex(RuntimeContractError, "more than once"):
            validate_adapter_manifest(manifest)

    def test_capability_negotiation_fails_closed(self) -> None:
        negotiate_capabilities("claude-code", ["agent-delegation"])
        with self.assertRaisesRegex(RuntimeContractError, "lacks"):
            negotiate_capabilities("copilot-cli", ["agent-delegation"])
        with self.assertRaisesRegex(RuntimeContractError, "Unknown runtime"):
            negotiate_capabilities("unknown", [])

    def test_runtime_auto_detection_uses_priority_and_winget_fallback(self) -> None:
        with mock.patch(
            "review_runtime.shutil.which",
            side_effect=lambda command: "C:/tools/codex.exe" if command == "codex" else None,
        ):
            self.assertEqual("codex", resolve_runtime("auto"))
        with (
            mock.patch("review_runtime.shutil.which", return_value=None),
            mock.patch("review_runtime.Path.is_file", return_value=True),
        ):
            self.assertEqual("copilot-cli", resolve_runtime("auto"))
        with (
            mock.patch("review_runtime.shutil.which", return_value=None),
            mock.patch("review_runtime.Path.is_file", return_value=False),
            self.assertRaisesRegex(RuntimeContractError, "No supported"),
        ):
            resolve_runtime("auto")

    def test_runtime_auto_prefers_the_stated_host_over_path(self) -> None:
        # A Codex session on a machine that also has claude installed must not resolve to claude-code.
        with mock.patch("review_runtime.shutil.which", return_value="C:/tools/claude.exe") as which:
            self.assertEqual("codex", resolve_runtime("auto", "codex"))
            self.assertEqual("copilot-cli", resolve_runtime("auto", "copilot-cli"))
            which.assert_not_called()
            self.assertEqual("claude-code", resolve_runtime("auto", None), "no host keeps the PATH search")
        with self.assertRaisesRegex(RuntimeContractError, "Unknown runtime host"):
            resolve_runtime("auto", "cursor")

    def test_configured_runtime_overrides_the_stated_host(self) -> None:
        with mock.patch("review_runtime.shutil.which", return_value=None):
            self.assertEqual("copilot-cli", resolve_runtime("copilot-cli", "claude-code"))
            self.assertEqual("codex", resolve_runtime("codex", None))

    def test_git_symlink_entries_are_rejected_before_materialization(self) -> None:
        manifest = self._manifest()

        def runner(arguments: list[str]) -> CommandResult:
            if "ls-tree" in arguments:
                return CommandResult(
                    0,
                    f"120000 blob {'a' * 40}\t{manifest['entrypoint']}\n",
                    "",
                )
            raise AssertionError(f"Unexpected command: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "materialized"
            with self.assertRaisesRegex(RuntimeContractError, "not a regular file"):
                materialize_reviewer(
                    Path(temporary) / "checkout",
                    "b" * 40,
                    manifest,
                    destination,
                    runner=runner,
                )
            self.assertFalse(destination.exists())

    def test_reviewer_is_loaded_from_trusted_commit_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, trusted, head = self._repository(root)
            verify_checkout_remote(checkout, "example/one")
            resolved = resolve_reviewer_commit(checkout, trusted, head_sha=head)
            self.assertEqual(trusted, resolved)
            manifest = load_manifest_from_commit(checkout, trusted, ".review/adapter.json")
            destination = root / "materialized"
            hashes = materialize_reviewer(checkout, trusted, manifest, destination)
            self.assertEqual(3, len(hashes))
            self.assertEqual(
                "# Trusted fixture\n",
                (destination / manifest["entrypoint"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                trusted, json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["source_commit"]
            )
            self.assertEqual(
                manifest["entrypoint"],
                json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["entrypoint"],
            )

    def test_trusted_files_are_materialized_as_their_exact_committed_bytes(self) -> None:
        crlf, latin1 = b"line one\r\nline two\r\n", b"caf\xe9\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, trusted, _ = self._repository(root)
            manifest = self._manifest()
            for relative, content in ((manifest["entrypoint"], crlf), (manifest["resources"][0], latin1)):
                (checkout / relative).write_bytes(content)
            # Not autocrlf: the blob must hold the CRLF itself, whatever the machine's Git configuration says.
            self._git(
                checkout, "-c", "core.autocrlf=false", "add", "--", manifest["entrypoint"], manifest["resources"][0]
            )
            self._git(
                checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "bytes"
            )
            commit = self._git(checkout, "rev-parse", "HEAD")
            self.assertEqual(
                crlf,
                review_runtime._read_git_file(
                    checkout, commit, manifest["entrypoint"], review_runtime.subprocess_runner
                ),
            )
            self.assertEqual(
                latin1,
                review_runtime._read_git_file(
                    checkout, commit, manifest["resources"][0], review_runtime.subprocess_runner
                ),
            )
            destination = root / "materialized"
            hashes = materialize_reviewer(checkout, commit, manifest, destination)
            self.assertEqual(crlf, (destination / manifest["entrypoint"]).read_bytes())
            self.assertEqual(latin1, (destination / manifest["resources"][0]).read_bytes())
            expected = {
                manifest["entrypoint"]: hashlib.sha256(b"line one\r\nline two\r\n").hexdigest(),
                manifest["resources"][0]: hashlib.sha256(b"caf\xe9\n").hexdigest(),
                manifest["agent_profiles"][0]: hashlib.sha256(b"Agent\n").hexdigest(),
            }
            self.assertEqual(expected, hashes)
            self.assertEqual(
                expected,
                json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["source_hashes"],
            )
            self.assertNotEqual(trusted, commit)

    def test_the_git_runner_decodes_in_the_caller_and_keeps_every_byte(self) -> None:
        # Decoding in subprocess.run happens in a reader thread on Windows, where a bad byte leaves stdout None, and
        # in the caller on POSIX. Capturing bytes and decoding here gives both platforms the same, lossless path.
        completed = subprocess.CompletedProcess(["git"], 0, b"caf\xe9\r\nok\r\n", b"fatal: caf\xe9\n")
        with mock.patch("review_runtime.subprocess.run", return_value=completed) as run:
            result = review_runtime.subprocess_runner(["git", "show", "HEAD:latin1.md"])
        options = run.call_args.kwargs
        self.assertTrue(options.get("capture_output"))
        for decoding in ("text", "encoding", "errors", "universal_newlines"):
            self.assertNotIn(decoding, options)
        self.assertEqual(b"caf\xe9\r\nok\r\n", result.stdout.encode("utf-8", "surrogateescape"))
        self.assertEqual("fatal: caf�\n", result.stderr)
        self.assertEqual(0, result.returncode)

    def test_a_git_failure_never_carries_an_undecodable_byte_into_its_message(self) -> None:
        def runner(arguments: list[str]) -> review_runtime.CommandResult:
            return review_runtime.CommandResult(128, "caf\udce9", "")

        with self.assertRaises(RuntimeContractError) as raised:
            review_runtime._run_git(Path("checkout"), runner, "rev-parse", "HEAD")
        self.assertEqual("caf�", str(raised.exception))

    def test_source_snapshot_uses_exact_head_and_excludes_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, head = self._repository(root)
            (checkout / "CLAUDE.md").write_text("Untrusted instructions\n", encoding="utf-8")
            (checkout / "AGENTS.override.md").write_text("Untrusted override\n", encoding="utf-8")
            (checkout / "src").mkdir()
            (checkout / "src" / "Example.cs").write_text("class Example {}\n", encoding="utf-8")
            (checkout / "src" / "AGENTS.override.md").write_text("Nested override\n", encoding="utf-8")
            (checkout / "src" / "CLAUDE.local.md").write_text("Nested local instructions\n", encoding="utf-8")
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "source",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertTrue((destination / "src" / "Example.cs").is_file())
            self.assertFalse((destination / "CLAUDE.md").exists())
            self.assertFalse((destination / ".claude").exists())
            self.assertEqual("agent-instruction", metadata["excluded_paths"]["CLAUDE.md"])
            for override in ("AGENTS.override.md", "src/AGENTS.override.md", "src/CLAUDE.local.md"):
                self.assertFalse((destination / override).exists())
                self.assertEqual("agent-instruction", metadata["excluded_paths"][override])
            self.assertIn(
                ".claude/skills/fixture-review/SKILL.md",
                metadata["excluded_paths"],
            )
            verify_source_snapshot(
                destination,
                expected_repository="example/one",
                expected_commit=head,
            )
            (destination / "src" / "Example.cs").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "hash mismatch"):
                verify_source_snapshot(
                    destination,
                    expected_repository="example/one",
                    expected_commit=head,
                )
            # The structural check skips content but still holds the file set to the manifest.
            arguments = {"expected_repository": "example/one", "expected_commit": head, "contents": False}
            verify_source_snapshot(destination, **arguments)
            (destination / "src" / "Extra.cs").write_text("class Extra {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, r"extra=\['src/Extra.cs'\]"):
                verify_source_snapshot(destination, **arguments)
            (destination / "src" / "Extra.cs").unlink()
            (destination / "src" / "Example.cs").unlink()
            for contents in (False, True):
                with self.assertRaisesRegex(RuntimeContractError, "file is missing: src/Example.cs"):
                    verify_source_snapshot(destination, **{**arguments, "contents": contents})

    def test_source_snapshot_writes_more_files_than_its_write_queue_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            self._git(checkout, "init", "-b", "main")
            self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
            # More files than SNAPSHOT_WRITE_WORKERS * 4 writes in flight, across several directories.
            count = review_runtime.SNAPSHOT_WRITE_WORKERS * 4 * 3 + 1
            for index in range(count):
                target = checkout / f"dir{index % 7}" / f"file{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"content {index}\n" * (index + 1), encoding="utf-8")
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "many files",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertEqual(count, len(metadata["source_hashes"]))
            # The full content check re-reads every written file against the hashes computed before writing.
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)

    @staticmethod
    def _archive_runner(commit: str, entries: list[tuple[str, bytes, str]]):
        """A git runner whose `archive` writes `entries`, each (name, tar type, link name), as a tar."""

        def runner(arguments: list[str]) -> CommandResult:
            if arguments[-3:] == ["remote", "get-url", "origin"]:
                return CommandResult(0, "https://github.com/example/one.git\n", "")
            if "rev-parse" in arguments:
                return CommandResult(0, commit + "\n", "")
            if "archive" in arguments:
                output = next(item.split("=", 1)[1] for item in arguments if item.startswith("--output="))
                with tarfile.open(output, mode="w") as archive:
                    for name, kind, link in entries:
                        entry = tarfile.TarInfo(name)
                        entry.type = kind
                        if kind == tarfile.REGTYPE:
                            entry.size = len(link.encode("utf-8"))
                            archive.addfile(entry, io.BytesIO(link.encode("utf-8")))
                        else:
                            entry.linkname = link
                            archive.addfile(entry)
                return CommandResult(0, "", "")
            raise AssertionError(f"Unexpected command: {arguments}")

        return runner

    def test_source_snapshot_excludes_links_and_other_non_regular_entries(self) -> None:
        commit = "b" * 40
        runner = self._archive_runner(
            commit,
            [
                ("src/A.cs", tarfile.REGTYPE, "class A {}\n"),
                ("tools/cache", tarfile.SYMTYPE, "/home/dev/.cache/tool"),
                ("src/Hard.cs", tarfile.LNKTYPE, "src/A.cs"),
                ("run/pipe", tarfile.FIFOTYPE, ""),
                ("dev/tty", tarfile.CHRTYPE, ""),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            metadata = materialize_source_snapshot(
                Path(temporary) / "checkout",
                "example/one",
                commit,
                destination,
                runner=runner,
            )
            self.assertEqual(
                {
                    "tools/cache": "symbolic-link",
                    "src/Hard.cs": "non-regular",
                    "run/pipe": "non-regular",
                    "dev/tty": "non-regular",
                },
                metadata["excluded_paths"],
            )
            self.assertEqual(["src/A.cs"], list(metadata["source_hashes"]))
            self.assertEqual(
                sorted([SOURCE_SNAPSHOT_MANIFEST, "src/A.cs"]),
                sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()),
            )
            self.assertFalse((destination / "tools").exists(), "nothing is written for a link, not even its folder")
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=commit)
            # Deliberate, like binary and agent-instruction: reviewed from the diff, never a coverage gap.
            diff = Path(temporary) / "diff.patch"
            diff.write_text(
                "diff --git a/tools/cache b/tools/cache\nnew file mode 120000\nindex 0000000..1111111\n"
                "--- /dev/null\n+++ b/tools/cache\n@@ -0,0 +1 @@\n+/home/dev/.cache/tool\n"
                "\\ No newline at end of file\n"
                "diff --git a/run/pipe b/run/pipe\nnew file mode 100644\nindex 0000000..2222222\n"
                "--- /dev/null\n+++ b/run/pipe\n@@ -0,0 +1 @@\n+x\n",
                encoding="utf-8",
            )
            self.assertEqual([], review_runtime.unavailable_sources(diff, metadata))
        self.assertEqual(
            {"agent-instruction", "binary", "file-size-limit", "unsafe-path", "symbolic-link", "non-regular"},
            review_runtime.SNAPSHOT_EXCLUSION_REASONS,
        )
        self.assertEqual({"file-size-limit", "unsafe-path"}, review_runtime.COVERAGE_GAP_REASONS)

    def test_source_snapshot_still_refuses_a_link_at_the_reserved_manifest_path(self) -> None:
        commit = "b" * 40
        runner = self._archive_runner(commit, [(SOURCE_SNAPSHOT_MANIFEST, tarfile.SYMTYPE, "/etc/passwd")])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with self.assertRaisesRegex(RuntimeContractError, "reserved snapshot path"):
                materialize_source_snapshot(
                    Path(temporary) / "checkout",
                    "example/one",
                    commit,
                    destination,
                    runner=runner,
                )
            self.assertFalse(destination.exists())

    def test_source_snapshot_verification_rejects_instruction_paths_and_limits(self) -> None:
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot = root / "snapshot"
            snapshot.mkdir()
            instruction = snapshot / "CLAUDE.md"
            content = b"Untrusted instructions\n"
            instruction.write_bytes(content)
            manifest = {
                "schema_version": 1,
                "repository": "example/one",
                "source_commit": commit,
                "source_hashes": {"CLAUDE.md": hashlib.sha256(content).hexdigest()},
                "excluded_paths": {},
            }
            (snapshot / SOURCE_SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "agent-instruction"):
                verify_source_snapshot(
                    snapshot,
                    expected_repository="example/one",
                    expected_commit=commit,
                )

            instruction.rename(snapshot / "source.txt")
            manifest["source_hashes"] = {"source.txt": hashlib.sha256(content).hexdigest()}
            (snapshot / SOURCE_SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            with (
                mock.patch.object(review_runtime, "MAX_CHANGED_FILE_BYTES", 1),
                self.assertRaisesRegex(RuntimeContractError, "file exceeds"),
            ):
                verify_source_snapshot(
                    snapshot,
                    expected_repository="example/one",
                    expected_commit=commit,
                )

    def test_source_snapshot_materialization_enforces_file_count_limit(self) -> None:
        commit = "b" * 40

        def runner(arguments: list[str]) -> CommandResult:
            if arguments[-3:] == ["remote", "get-url", "origin"]:
                return CommandResult(0, "https://github.com/example/one.git\n", "")
            if "rev-parse" in arguments:
                return CommandResult(0, commit + "\n", "")
            if "archive" in arguments:
                output = next(item.split("=", 1)[1] for item in arguments if item.startswith("--output="))
                with tarfile.open(output, mode="w") as archive:
                    for name in ("one.txt", "two.txt"):
                        path = Path(temporary) / name
                        path.write_bytes(b"x")
                        archive.add(path, arcname=name)
                return CommandResult(0, "", "")
            raise AssertionError(f"Unexpected command: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with (
                mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_FILES", 1),
                self.assertRaisesRegex(RuntimeContractError, "file-count"),
            ):
                materialize_source_snapshot(
                    Path(temporary) / "checkout",
                    "example/one",
                    commit,
                    destination,
                    runner=runner,
                )
            self.assertFalse(destination.exists())

    def test_source_snapshot_excludes_binary_and_oversized_files_from_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, _ = self._repository(root)
            (checkout / "assets").mkdir()
            (checkout / "assets" / "image.bin").write_bytes(b"PNG\0" + b"x" * 32)
            (checkout / "assets" / "large.txt").write_bytes(b"y" * 65)
            (checkout / "src.txt").write_bytes(b"z" * 8)
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "assets",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            with (
                mock.patch.object(review_runtime, "MAX_SOURCE_FILE_BYTES", 64),
                mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_BYTES", 64),
            ):
                metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
                verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            self.assertEqual("binary", metadata["excluded_paths"]["assets/image.bin"])
            self.assertEqual("file-size-limit", metadata["excluded_paths"]["assets/large.txt"])
            self.assertFalse((destination / "assets").exists())
            self.assertTrue((destination / "src.txt").is_file())

    def test_adapter_request_requires_verified_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, head = self._repository(root)
            snapshot = root / "snapshot"
            materialize_source_snapshot(checkout, "example/one", head, snapshot)
            diff = root / "diff.patch"
            diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
            request = build_adapter_request(
                mode="initial",
                repository="example/one",
                pull_number=12,
                base_ref="main",
                base_sha="a" * 40,
                head_sha=head,
                title="Fixture",
                url="https://github.com/example/one/pull/12",
                diff_path=diff,
                source_snapshot_root=snapshot,
            )
            self.assertEqual(str(snapshot), request["source_snapshot"]["root"])
            self.assertEqual(head, request["source_snapshot"]["source_commit"])
            extra = snapshot / "extra.txt"
            extra.write_text("not declared", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "file set mismatch"):
                build_adapter_request(
                    mode="initial",
                    repository="example/one",
                    pull_number=12,
                    base_ref="main",
                    base_sha="a" * 40,
                    head_sha=head,
                    title="Fixture",
                    url="https://github.com/example/one/pull/12",
                    diff_path=diff,
                    source_snapshot_root=snapshot,
                )

    def test_pull_ref_head_and_remote_mismatch_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout, trusted, head = self._repository(Path(temporary))
            with self.assertRaisesRegex(RuntimeContractError, "Pull-request refs"):
                resolve_reviewer_commit(checkout, "refs/pull/1/head", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "pull-request head"):
                resolve_reviewer_commit(checkout, "HEAD", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "origin mismatch"):
                verify_checkout_remote(checkout, "different/one")


class ReviewOperationTests(unittest.TestCase):
    @staticmethod
    def _pull(number: int, state: str = "OPEN", merged_at: str | None = None) -> dict:
        return {
            "number": number,
            "title": f"Pull {number}",
            "url": f"https://github.com/example/one/pull/{number}",
            "state": state,
            "isDraft": False,
            "baseRefName": "main",
            "baseRefOid": "a" * 40,
            "headRefOid": chr(98 + number) * 40,
            "headRefName": f"feature-{number}",
            "mergedAt": merged_at,
        }

    def test_single_pull_canary_selector_is_explicit_and_bounded(self) -> None:
        self.assertEqual(("example/one", 42), parse_pull_selector("Example/One#42"))
        for value in ("example/one", "example/one#0", "#42", "example/one#42#43"):
            with self.subTest(value=value), self.assertRaises(ReviewOperationError):
                parse_pull_selector(value)
        selected = validate_canary_pull(self._pull(42), repository="example/one", number=42)
        self.assertEqual(42, selected["number"])
        draft = self._pull(42)
        draft["isDraft"] = True
        self.assertTrue(validate_canary_pull(draft, repository="example/one", number=42)["isDraft"])
        with self.assertRaisesRegex(ReviewOperationError, "does not match"):
            validate_canary_pull(self._pull(41), repository="example/one", number=42)

    def test_selection_skips_current_heads_and_old_merges(self) -> None:
        pulls = [
            self._pull(1),
            self._pull(2, "MERGED", "2025-12-01T00:00:00Z"),
            self._pull(3, "MERGED", "2026-01-03T00:00:00Z"),
        ]
        selected = select_eligible_pulls(
            pulls,
            merged_since=date(2026, 1, 1),
            reviewed_heads={1: "c" * 40},
        )
        self.assertEqual([3], [item["number"] for item in selected])

    def test_watermark_retains_unreviewed_and_incomplete_work(self) -> None:
        merged = [
            self._pull(2, "MERGED", "2026-01-05T00:00:00Z"),
            self._pull(3, "MERGED", "2026-01-08T00:00:00Z"),
        ]
        previous = date(2026, 1, 1)
        self.assertEqual(
            date(2026, 1, 7),
            safe_watermark(
                previous=previous,
                today=date(2026, 1, 10),
                eligible_merged=merged,
                completed_numbers={2},
                enumeration_complete=True,
            ),
        )
        self.assertEqual(
            previous,
            safe_watermark(
                previous=previous,
                today=date(2026, 1, 10),
                eligible_merged=merged,
                completed_numbers={2, 3},
                enumeration_complete=False,
            ),
        )

    def test_generic_and_specialized_results_share_record_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {
                "protocol_version": 1,
                "mode": "initial",
                "repository": "example/one",
                "pull_number": 12,
                "pull_request": {
                    "title": "Improve behavior",
                    "url": "https://github.com/example/one/pull/12",
                    "base_ref": "main",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                "diff_path": str(root / "diff.patch"),
                "prior_findings": [],
                "github_comments": [],
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path = root / "result.json"
            result_path.write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
            archive = root / "archive"
            json_path, markdown_path, persisted = commit_adapter_result(
                request_path=request_path,
                result_path=result_path,
                archive_root=archive,
                policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                adapter={
                    "name": "repository-specialist",
                    "scope": "repository",
                    "source_commit": "c" * 40,
                    "source_hashes": {"SKILL.md": "d" * 64},
                },
            )
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertEqual("F001", persisted["findings"][0]["id"])

    def test_archive_retry_reuses_validated_pending_local_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {
                "protocol_version": 1,
                "mode": "initial",
                "repository": "example/one",
                "pull_number": 12,
                "pull_request": {
                    "title": "Improve behavior",
                    "url": "https://github.com/example/one/pull/12",
                    "base_ref": "main",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                "diff_path": str(root / "diff.patch"),
                "prior_findings": [],
                "github_comments": [],
            }
            request_path = root / "request.json"
            result_path = root / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path.write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
            archive = root / "archive"
            archive.write_text("blocks directory creation", encoding="utf-8")
            local = root / "local"
            arguments = {
                "request_path": request_path,
                "result_path": result_path,
                "archive_root": archive,
                "local_mirror_root": local,
                "policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                "adapter": {
                    "name": "generic",
                    "scope": "generic",
                    "source_commit": None,
                    "source_hashes": {},
                },
            }
            with self.assertRaises(PersistenceError):
                commit_adapter_result(**arguments)
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            archive.unlink()
            json_path, _, persisted = commit_adapter_result(**arguments)
            self.assertTrue(json_path.is_file())
            self.assertEqual(
                latest_record(local, "example/one", 12)["artifacts"],
                persisted["artifacts"],
            )

    def test_latest_reviewed_heads_ignores_missing_pull_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = validate_adapter_result(
                valid_adapter_result(),
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            )
            record = build_record(
                valid_request(),
                result,
                version=1,
                policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            )
            commit_record(
                root,
                "example/one",
                12,
                record,
                expected_latest_version=None,
            )
            self.assertEqual(
                {12: "b" * 40},
                latest_reviewed_heads(root, "example/one", [12, 13]),
            )


class RuntimeHostTests(unittest.TestCase):
    @staticmethod
    def _materialized_reviewer(root: Path) -> tuple[Path, Path]:
        materialized = root / "trusted"
        materialized.mkdir()
        entrypoint = materialized / "SKILL.md"
        content = b"# Trusted reviewer\n"
        entrypoint.write_bytes(content)
        rules = materialized / "rules.md"
        rules_content = b"# Trusted rules\n"
        rules.write_bytes(rules_content)
        (materialized / "materialization.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "fixture",
                    "entrypoint": "SKILL.md",
                    "source_commit": "a" * 40,
                    "source_hashes": {
                        "SKILL.md": hashlib.sha256(content).hexdigest(),
                        "rules.md": hashlib.sha256(rules_content).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        return materialized, entrypoint

    @staticmethod
    def _write_request_with_snapshot(root: Path, request: Path) -> Path:
        source = root / "source"
        source.mkdir()
        source_file = source / "Example.cs"
        source_content = b"class Example {}\n"
        source_file.write_bytes(source_content)
        head_sha = "b" * 40
        manifest_path = source / SOURCE_SNAPSHOT_MANIFEST
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repository": "example/one",
                    "source_commit": head_sha,
                    "source_hashes": {"Example.cs": hashlib.sha256(source_content).hexdigest()},
                    "excluded_paths": {},
                }
            ),
            encoding="utf-8",
        )
        diff_path = root / "diff.patch"
        diff_path.write_text("diff --git a/Example.cs b/Example.cs\n", encoding="utf-8")
        request.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "mode": "initial",
                    "repository": "example/one",
                    "pull_number": 12,
                    "pull_request": {
                        "title": "Fixture",
                        "url": "https://github.com/example/one/pull/12",
                        "base_ref": "main",
                        "base_sha": "a" * 40,
                        "head_sha": head_sha,
                    },
                    "diff_path": str(diff_path),
                    "source_snapshot": {
                        "root": str(source),
                        "manifest_path": str(manifest_path),
                        "source_commit": head_sha,
                    },
                    "prior_findings": [],
                    "github_comments": [],
                }
            ),
            encoding="utf-8",
        )
        return source

    def test_copilot_command_is_noninteractive_and_least_privilege(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            command = copilot_command(
                "copilot",
                prompt="Review",
                run_directory=root,
                materialized_root=root,
                result_path=root / "result.json",
            )
            self.assertIn("--no-custom-instructions", command)
            self.assertIn("--no-ask-user", command)
            self.assertIn("--disable-builtin-mcps", command)
            self.assertIn("--disallow-temp-dir", command)
            self.assertIn("--deny-tool=shell", command)
            self.assertIn("--deny-tool=url", command)
            self.assertNotIn("--allow-all", command)
            available_tools = command.index("--available-tools")
            self.assertEqual(
                ["view", "grep", "glob", "edit", "create"],
                command[available_tools + 1 : available_tools + 6],
            )
            self.assertIn(f"--allow-tool=write({(root / 'result.json').as_posix()})", command)

    def test_copilot_rejects_result_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_directory = root / "run"
            run_directory.mkdir()
            with self.assertRaisesRegex(RuntimeContractError, "result path must be inside the run directory"):
                copilot_command(
                    "copilot",
                    prompt="Review",
                    run_directory=run_directory,
                    materialized_root=root,
                    result_path=root / "result.json",
                )

    def test_copilot_rejects_diff_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_directory = root / "run"
            run_directory.mkdir()
            materialized, _ = self._materialized_reviewer(root)
            request = run_directory / "request.json"
            self._write_request_with_snapshot(run_directory, request)
            external_diff = root / "external.diff"
            external_diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
            payload = json.loads(request.read_text(encoding="utf-8"))
            payload["diff_path"] = str(external_diff)
            request.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "diff must be a file inside"):
                run_copilot(
                    run_directory=run_directory,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=run_directory / "result.json",
                    diagnostic_path=run_directory / "diagnostic.jsonl",
                    isolation_root=run_directory / "isolation",
                    executable="copilot",
                )

    def test_copilot_version_parser_and_winget_fallback(self) -> None:
        self.assertEqual((1, 0, 88), parse_copilot_version("GitHub Copilot CLI 1.0.88"))
        with tempfile.TemporaryDirectory() as temporary:
            local_app_data = Path(temporary)
            winget = local_app_data / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
            winget.parent.mkdir(parents=True)
            winget.write_bytes(b"fixture")
            with (
                mock.patch("review_hosts.shutil.which", return_value=None),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}),
            ):
                self.assertEqual(str(winget), find_copilot())

    def test_copilot_jsonl_is_diagnostic_not_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, entrypoint = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            result_path = root / "result.json"
            staging = root / "copilot-result-1.json"
            diagnostic = root / "diagnostic.jsonl"
            isolation = root / "isolation"
            invocations: list[tuple[list[str], Path, dict[str, str]]] = []

            def runner(arguments: list[str], cwd: Path, environment: dict[str, str]) -> ProcessResult:
                invocations.append((list(arguments), cwd, dict(environment)))
                if "--version" in arguments:
                    return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                self.assertFalse(result_path.exists())
                staging.write_text('{"protocol_version": 1}', encoding="utf-8")
                return ProcessResult(0, '{"type":"assistant.message","text":"done"}\n', "")

            result = run_copilot(
                run_directory=root,
                materialized_root=materialized,
                request_path=request,
                result_path=result_path,
                staging_path=staging,
                diagnostic_path=diagnostic,
                isolation_root=isolation,
                runner=runner,
                executable="copilot",
                base_environment={"GH_TOKEN": "test-token"},
            )
            self.assertEqual("GitHub Copilot CLI 1.2.3", result.version)
            self.assertIn("assistant.message", diagnostic.read_text(encoding="utf-8"))
            self.assertEqual({"protocol_version": 1}, json.loads(result_path.read_text()))
            self.assertFalse(staging.exists(), "the staging file is renamed into place, never copied")
            command, cwd, environment = invocations[-1]
            # Copilot may write only its staging file; the result appears whole, by rename, or not at all.
            self.assertIn(f"--allow-tool=write({staging.as_posix()})", command)
            self.assertNotIn(f"--allow-tool=write({result_path.as_posix()})", command)
            self.assertIn(str(staging), command[-1])
            self.assertEqual(isolation / "workspace", cwd)
            self.assertEqual(str(isolation / "home"), environment["HOME"])
            self.assertEqual(str(isolation / "home"), environment["USERPROFILE"])
            self.assertEqual(str(isolation / "copilot-home"), environment["COPILOT_HOME"])
            self.assertEqual("test-token", environment["GH_TOKEN"])
            self.assertIn(str(entrypoint.resolve()), command[-1])

    def test_copilot_result_is_promoted_only_when_valid_and_allowed(self) -> None:
        for case in ("partial", "refused"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                materialized, _ = self._materialized_reviewer(root)
                request = root / "request.json"
                self._write_request_with_snapshot(root, request)
                result_path = root / "result.json"
                staging = root / "copilot-result-1.json"
                promoted: list[tuple[Path, Path]] = []

                def runner(
                    arguments: list[str],
                    cwd: Path,
                    environment: dict[str, str],
                    *,
                    case: str = case,
                    staging: Path = staging,
                ) -> ProcessResult:
                    del cwd, environment
                    if "--version" in arguments:
                        return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                    content = '{"protocol_version": 1, "fin' if case == "partial" else '{"protocol_version": 1}'
                    staging.write_text(content, encoding="utf-8")
                    return ProcessResult(0, "", "")

                def promote(source: Path, target: Path, *, promoted: list[tuple[Path, Path]] = promoted) -> bool:
                    promoted.append((source, target))
                    return False

                error, reason = (
                    (RuntimeContractError, "not valid JSON") if case == "partial" else (HostSuperseded, "set aside")
                )
                with self.assertRaisesRegex(error, reason):
                    run_copilot(
                        run_directory=root,
                        materialized_root=materialized,
                        request_path=request,
                        result_path=result_path,
                        staging_path=staging,
                        promote=promote,
                        diagnostic_path=root / "diagnostic.jsonl",
                        isolation_root=root / "isolation",
                        runner=runner,
                        executable="copilot",
                    )
                self.assertFalse(result_path.exists())
                self.assertTrue(staging.is_file(), "a result never promoted stays in its staging file")
                self.assertEqual([] if case == "partial" else [(staging, result_path)], promoted)

    def test_copilot_rejects_modified_or_extra_reviewer_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            (materialized / "rules.md").write_text("# Modified rules\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "hash does not match"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            (materialized / "extra.md").write_text("# Extra\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "file set"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

    def test_copilot_rejects_old_version_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)

            def runner(arguments: list[str], cwd: Path, environment: dict[str, str]) -> ProcessResult:
                del cwd, environment
                self.assertIn("--version", arguments)
                return ProcessResult(0, "GitHub Copilot CLI 1.0.87\n", "")

            with self.assertRaisesRegex(RuntimeContractError, "1.0.88 or newer"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    runner=runner,
                    executable="copilot",
                )

    def test_copilot_timeout_is_a_contract_error_with_its_partial_output(self) -> None:
        for timed_out, partial in (("review", b'{"type":"assistant.message"}\n'), ("version", None)):
            with self.subTest(timed_out=timed_out), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                materialized, _ = self._materialized_reviewer(root)
                request = root / "request.json"
                self._write_request_with_snapshot(root, request)
                diagnostic = root / "diagnostic.jsonl"

                def runner(
                    arguments: list[str],
                    cwd: Path,
                    environment: dict[str, str],
                    *,
                    timed_out: str = timed_out,
                    partial: bytes | None = partial,
                ) -> ProcessResult:
                    del cwd, environment
                    if "--version" in arguments and timed_out == "review":
                        return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                    raise subprocess.TimeoutExpired(arguments, 1800, output=partial, stderr="still working")

                with self.assertRaisesRegex(
                    RuntimeContractError,
                    f"^GitHub Copilot CLI timed out after 1800s; see {re.escape(str(diagnostic))}$",
                ):
                    run_copilot(
                        run_directory=root,
                        materialized_root=materialized,
                        request_path=request,
                        result_path=root / "result.json",
                        diagnostic_path=diagnostic,
                        isolation_root=root / "isolation",
                        runner=runner,
                        executable="copilot",
                    )
                text = diagnostic.read_text(encoding="utf-8")
                if partial:
                    self.assertIn("assistant.message", text)
                self.assertIn("STDERR:\nstill working", text)
                self.assertFalse((root / "result.json").exists())

    def test_copilot_rejects_entrypoint_outside_materialized_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            metadata_path = materialized / "materialization.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["entrypoint"] = "../untrusted.md"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            (root / "untrusted.md").write_text("# Untrusted\n", encoding="utf-8")
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)

            with self.assertRaisesRegex(RuntimeContractError, "entrypoint is unsafe"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

    def test_copilot_rejects_modified_entrypoint_and_reused_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, entrypoint = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            entrypoint.write_text("# Modified reviewer\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeContractError, "hash does not match"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

            content = b"# Trusted reviewer\n"
            entrypoint.write_bytes(content)
            isolation = root / "isolation"
            isolation.mkdir()
            (isolation / "ambient-config.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "must be empty"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=isolation,
                    executable="copilot",
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import bounded_process
import github_client
import review_github
import review_io
import review_runtime
import review_state
import skill_roots
from executable_locator import located_command
from git_client import GitError, GitResult
from git_test_base import GitTestCase
from github_client import CommandResult
from review_archive import (
    ArchiveError,
    commit_record,
    latest_record,
    list_versions,
    pull_directory,
    record_files,
)
from review_config import (
    ConfigurationError,
    ConfigurationFieldError,
    default_manifest_path,
    resolve_repositories,
    validate_config,
    write_config,
)
from review_flags import FlagAlreadyResolved, FlagError, add_flag, edit_flag, load_store, resolve_flag
from review_github import GitHubClient, GitHubError, GitHubFieldError, GitHubInputError
from review_hosts import (
    HostSuperseded,
    ProcessResult,
    copilot_command,
    find_copilot,
    parse_copilot_version,
    run_copilot,
)
from review_io import (
    PersistenceError,
    PersistenceFieldError,
    ResourceLock,
    atomic_write_json,
    map_in_order,
    read_json,
    working_path,
)
from review_operation import (
    ReviewOperationError,
    ReviewOperationFieldError,
    archive_base,
    commit_adapter_result,
    latest_reviewed_heads,
    merged_date,
    parse_pull_selector,
    safe_watermark,
    select_eligible_pulls,
    validate_canary_pull,
    validate_pull,
)
from review_process import ProcessStatus
from review_records import (
    READ,
    TITLE_MAXIMUM_LENGTH,
    RecordError,
    RecordFieldError,
    build_record,
    calculate_verdict,
    read_title,
    record_artifacts,
    render_markdown,
    valid_analyzer,
    validate_adapter_result,
    validate_record,
    validate_record_pair,
    write_record_pair,
)
from review_runtime import (
    SOURCE_SNAPSHOT_MANIFEST,
    PrepareGitRunner,
    RuntimeContractError,
    RuntimeContractFieldError,
    build_adapter_request,
    choose_dispatch,
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
from review_state import StateError, StateFieldError, empty_state, load_state, update_state, validate_state
from test_review_config_validation import ABSOLUTE_PATH, native

SCRIPT_DIRECTORY = Path(__file__).resolve().parent


@contextlib.contextmanager
def undeletable(path: Path) -> Iterator[None]:
    """Keep the file `path` from being deleted for the block: on Windows by holding it open, which denies its deletion,
    and elsewhere, where an open file holds nothing, by a mode on its folder that denies removing what it holds."""
    if sys.platform == "win32":
        with path.open("rb"):
            yield
        return
    path.parent.chmod(0o555)
    try:
        yield
    finally:
        path.parent.chmod(0o755)


# What a typed-field reader expects of a git object id, and what it finds in one with a character outside 0-9a-f.
OBJECT_ID = "a lowercase hexadecimal digest of 40 or 64 characters"
NOT_HEX = "a string with a character that is not a lowercase hexadecimal digit"
INVALID_COMMITS = (
    ("main", "a string of 4 characters"),
    ("", "a string of 0 characters"),
    ("a" * 39, "a string of 39 characters"),
    ("a" * 41, "a string of 41 characters"),
    ("a" * 63, "a string of 63 characters"),
    ("a" * 65, "a string of 65 characters"),
    ("A" * 40, NOT_HEX),
    ("g" * 64, NOT_HEX),
    ("a" * 40 + "\n", "a string of 41 characters"),
)


MODEL_NAME_KEY = (
    "a key of config.model_names must be a non-empty trimmed single-line string of at most 200 characters, found"
)


REVIEWER_MODEL = "a non-empty trimmed single-line string of at most 200 characters"
SEVERITIES = 'one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION"'
DISPOSITIONS = 'one of "addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify"'


def field_message(path: str, expected: str, found: str) -> str:
    """The one message a field of the wrong kind raises, built from literal kinds rather than the reader's own."""
    return f"{path} must be {expected}, found {found}"


def field_pattern(path: str, expected: str, found: str) -> str:
    """`field_message` as a regular expression that matches the whole message."""
    return f"^{re.escape(field_message(path, expected, found))}$"


def frozen_clock(seconds: int) -> contextlib.AbstractContextManager[Any]:
    """The clock `utc_today` reads, stopped at `seconds` since the epoch."""
    return mock.patch.object(review_state, "time", mock.Mock(time=mock.Mock(return_value=seconds)))


def answering(result: CommandResult) -> Callable[[Sequence[str]], CommandResult]:
    """A command runner that answers every command with the result."""
    return lambda _arguments: result


def reporting(status: ProcessStatus) -> Callable[[int], ProcessStatus]:
    """A process probe that reports the status for every process."""
    return lambda _pid: status


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


class ConfigurationTests(GitTestCase):
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


class ParallelCallTests(GitTestCase):
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


class WorkingPathTests(GitTestCase):
    """working_path refuses a path in the skills directories skill-core names, and keeps any other path."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.own = self.root / "source skills"
        self.deployed = self.root / "home" / ".claude" / "skills"
        self.adapters = self.root / "home" / ".agents" / "skills"
        for patcher in (
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.own),
            mock.patch.object(skill_roots, "deployed_skill_roots", return_value=(self.deployed, self.adapters)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_path_inside_a_skills_directory_is_refused_naming_it(self) -> None:
        for root in (self.own, self.deployed, self.adapters):
            target = root / "review-prs" / "batch.json"
            with self.subTest(root=root), self.assertRaises(PersistenceError) as raised:
                working_path(target, "review-prs-batch-", "batch.json")
            self.assertEqual(
                f"{target} is inside the skills directory {root}; omit the option to write under a new temporary "
                "directory",
                str(raised.exception),
            )
            self.assertFalse(root.exists())

    def test_a_path_beside_the_skills_directories_is_kept(self) -> None:
        target = self.root / "home" / ".claude" / "batch.json"
        self.assertEqual(target, working_path(target, "review-prs-batch-", "batch.json"))

    def test_no_path_gives_the_name_in_a_new_temporary_directory(self) -> None:
        path = working_path(None, "review-prs-batch-", "batch.json")
        self.addCleanup(shutil.rmtree, path.parent)
        self.assertEqual("batch.json", path.name)
        self.assertTrue(path.parent.name.startswith("review-prs-batch-"), path)
        self.assertEqual([], list(path.parent.iterdir()))


class StateAndLockTests(GitTestCase):
    def test_state_update_writes_the_validated_replacement(self) -> None:
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

            self.assertEqual(initial, load_state(path))
            updated = update_state(path, advance)
            self.assertEqual(updated, load_state(path))
            self.assertNotEqual(initial, updated)
            with self.assertRaisesRegex(
                StateFieldError, '^state must be an object of known fields, found unknown field "extra"$'
            ):
                update_state(path, lambda state: {**state, "extra": True})
            self.assertEqual(updated, load_state(path), "a refused replacement leaves the state as it was")

    def test_a_watermark_must_begin_with_a_calendar_date(self) -> None:
        def state(value: str) -> dict:
            return {"schema_version": 1, "repositories": {"example/one": {"merged_since": value}}}

        for value in ("2026-03-10", "2026-03-10T00:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(state(value), validate_state(state(value)))
        # A week date and the basic form parse as ISO 8601 dates in Python, but the watermark is YYYY-MM-DD.
        for value in (
            "not-a-date",
            "",
            "2026-3-10",
            "2026-02-30",
            "2026-W10-1",
            "20260310",
            "\uff12\uff10\uff12\uff16-03-10",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    StateError, r"State example/one\.merged_since must begin with a YYYY-MM-DD date"
                ),
            ):
                validate_state(state(value))

    def test_contended_lock_fails_without_removing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            with ResourceLock(path):
                with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.01):
                    pass
                self.assertTrue(path.exists())

    BACKDATED_SECONDS = 61  # past the lock's grace period

    def _backdate(self, path: Path) -> int:
        """Move `path`'s modification time back BACKDATED_SECONDS and half a second more, so no rounding between the
        clocks reports less, and return it in nanoseconds."""
        old = time.time_ns() - (self.BACKDATED_SECONDS * 2 + 1) * 500_000_000
        os.utime(path, ns=(old, old))
        return old

    def _assert_reclaimed_after(self, warning: str, path: Path, reason: str, backdated: int) -> None:
        """`warning` is the one warning reclaiming the lock at `path` for `reason`, whose `{age}` is the age it
        reports, and that age is at least BACKDATED_SECONDS and no more than the time since `backdated`: a bound
        that moves with the runner's speed, never a fixed window."""
        stated = re.escape(reason).replace(re.escape("{age}"), r"(\d+)")
        match = re.fullmatch(rf"WARNING: Reclaimed the stale lock {re.escape(str(path))}: {stated}\.\n", warning)
        if match is None:
            self.fail(f"Not one warning reclaiming {path} for {reason!r}: {warning!r}")
        elapsed = (time.time_ns() - backdated) // 1_000_000_000
        self.assertGreaterEqual(int(match[1]), self.BACKDATED_SECONDS, warning)
        self.assertLessEqual(int(match[1]), elapsed + 1, warning)

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
        cases: dict[str, tuple[dict[str, Any], ProcessStatus]] = {
            "same process": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, 1234)),
            "no recorded start time": ({"pid": 4242}, ProcessStatus(True, 1234)),
            "recorded start time not an integer": ({"pid": 4242, "start_time": True}, ProcessStatus(True, 1)),
            "recorded start time a string": ({"pid": 4242, "start_time": "1234"}, ProcessStatus(True, 1)),
            "recorded start time a float": ({"pid": 4242, "start_time": 1234.0}, ProcessStatus(True, 1)),
            "start time unreadable": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, None)),
            "pid not positive": ({"pid": 0, "start_time": 1234}, ProcessStatus(False, None)),
            "pid a string": ({"pid": "4242", "start_time": 1234}, ProcessStatus(False, None)),
            "pid a boolean": ({"pid": True, "start_time": 1234}, ProcessStatus(False, None)),
            "pid a float": ({"pid": 4242.0, "start_time": 1234}, ProcessStatus(False, None)),
            "pid an object": ({"pid": {}, "start_time": 1234}, ProcessStatus(False, None)),
        }
        for name, (owner, status) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                self._old_lock(path, owner)
                with (
                    self.assertRaisesRegex(PersistenceError, "Timed out"),
                    ResourceLock(path, timeout_seconds=0.01, probe=reporting(status)),
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

    def test_recent_lock_of_live_owner_times_out_and_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            owner = {"pid": 4242, "token": "a" * 32, "created_unix": time.time(), "start_time": 1}
            atomic_write_json(path / "owner.json", owner)
            started = time.monotonic()
            with (
                self.assertRaisesRegex(
                    PersistenceError, re.escape(f"Timed out waiting for lock {path}; delete it if no review is running")
                ),
                ResourceLock(path, timeout_seconds=0.2, probe=reporting(ProcessStatus(True, 1))),
            ):
                pass
            self.assertGreaterEqual(time.monotonic() - started, 0.2)
            self.assertEqual(owner, read_json(path / "owner.json"))
            self.assertEqual(["resource.lock"], [item.name for item in Path(temporary).iterdir()])

    def test_recent_lock_of_dead_owner_is_reclaimed_with_a_warning(self) -> None:
        # A dead holder blocked every review of the repository for an hour.
        cases = {
            "dead": (ProcessStatus(False, None), "PID 4242 is not running"),
            "pid reused": (ProcessStatus(True, 5678), "PID 4242 now names another process"),
        }
        for name, (status, reason) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                path.mkdir()
                atomic_write_json(
                    path / "owner.json",
                    {"pid": 4242, "token": "a" * 32, "created_unix": time.time(), "start_time": 1234},
                )

                def probe(pid: int, *, status: ProcessStatus = status) -> ProcessStatus:
                    return ProcessStatus(True, 9999) if pid == os.getpid() else status

                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5, probe=probe):
                    self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
                self.assertEqual(f"WARNING: Reclaimed the stale lock {path}: {reason}.\n", errors.getvalue())
                self.assertEqual([], list(Path(temporary).iterdir()), "the stale lock is deleted")

    def test_lock_without_owner_file_is_reclaimed_after_the_grace_period(self) -> None:
        # A process killed between creating the lock and writing its owner left a lock nothing reclaimed.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.1):
                pass
            self.assertTrue(path.is_dir(), "a lock without an owner file is kept during the grace period")
            backdated = self._backdate(path)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5):
                self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
            self._assert_reclaimed_after(
                errors.getvalue(), path, "it has had no owner file for {age} seconds", backdated
            )
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_reclaim_puts_back_a_lock_another_process_took_meanwhile(self) -> None:
        # Moving the stale lock aside is the atomic step; a lock taken between the judgment and the move is not it.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            self._old_lock(path, {"pid": 4242, "start_time": 1234})
            taken = {"pid": 5151, "token": "b" * 32, "created_unix": time.time(), "start_time": 1}

            def probe(pid: int) -> ProcessStatus:
                if pid == 4242:
                    shutil.rmtree(path)  # another waiter reclaims the stale lock and takes it
                    path.mkdir()
                    atomic_write_json(path / "owner.json", taken)
                    return ProcessStatus(False, None)
                return ProcessStatus(True, 1)

            with (
                self.assertRaisesRegex(PersistenceError, "Timed out"),
                ResourceLock(path, timeout_seconds=0.1, probe=probe),
            ):
                pass
            self.assertEqual(taken, read_json(path / "owner.json"))
            self.assertEqual(["resource.lock"], [item.name for item in Path(temporary).iterdir()])

    def test_release_succeeds_while_a_waiter_reads_the_owner_file(self) -> None:
        # A waiter reading the owner file made the holder's release fail, and the lock outlived both sessions.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                lock = ResourceLock(path)
                lock.__enter__()
                with review_io.open_shared(path / "owner.json") as reader:
                    lock.__exit__(None, None, None)
                    self.assertFalse(path.exists(), "the lock is released while the owner file is open")
                    self.assertEqual(lock.token, json.loads(reader.read())["token"])
                with (
                    mock.patch.object(review_io.time, "sleep", side_effect=AssertionError("waited")),
                    ResourceLock(path),
                ):
                    pass
            self.assertEqual("", errors.getvalue())
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_release_that_cannot_remove_the_lock_warns_and_a_later_lock_reclaims_it(self) -> None:
        # The holder's work is done; a lock left behind by a process outside the protocol is stale once it exits.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                lock = ResourceLock(path)
                lock.__enter__()
                with undeletable(path / "owner.json"):
                    lock.__exit__(None, None, None)
            self.assertRegex(
                errors.getvalue(),
                rf"^WARNING: Could not remove the lock {re.escape(str(path))}: .+\. A later review reclaims it once "
                r"this process has exited\.\n$",
            )
            self.assertTrue((path / "owner.json").is_file(), "the lock is left behind")
            lock.__exit__(None, None, None)  # released once: nothing more to do

            def probe(pid: int) -> ProcessStatus:
                return ProcessStatus(False, None) if pid == os.getpid() else ProcessStatus(True, 1)

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5, probe=probe):
                pass
            self.assertEqual(
                f"WARNING: Reclaimed the stale lock {path}: PID {os.getpid()} is not running.\n", errors.getvalue()
            )
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_release_still_refuses_a_lock_another_session_took(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            lock = ResourceLock(path)
            lock.__enter__()
            atomic_write_json(path / "owner.json", {"pid": os.getpid(), "token": "b" * 32})
            with self.assertRaisesRegex(PersistenceError, "Lock ownership changed before release"):
                lock.__exit__(None, None, None)
            self.assertEqual("b" * 32, read_json(path / "owner.json")["token"])

    def test_release_refuses_an_owner_record_of_the_wrong_kind(self) -> None:
        for content, found in (([], "an array"), ("owner", 'string "owner"'), (None, "null"), (7, "integer 7")):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                lock = ResourceLock(path)
                lock.__enter__()
                atomic_write_json(path / "owner.json", content)
                with self.assertRaisesRegex(
                    PersistenceError, f"^Lock ownership changed before release: {re.escape(str(path))}$"
                ) as context:
                    lock.__exit__(None, None, None)
                self.assertIsInstance(context.exception.__cause__, PersistenceFieldError)
                self.assertEqual(f"lock owner must be an object, found {found}", str(context.exception.__cause__))
                self.assertEqual(content, read_json(path / "owner.json"), "a refused release leaves the lock")

    def test_waiter_takes_a_lock_whose_owner_file_vanishes_while_it_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            atomic_write_json(path / "owner.json", {"pid": 4242, "token": "a" * 32, "start_time": 1})
            opened: list[Path] = []

            def release_first(owner: Path) -> object:
                opened.append(owner)
                shutil.rmtree(path)  # the holder releases the lock between the waiter's stat and its read
                raise FileNotFoundError(2, "gone", str(owner))

            lock = ResourceLock(path, timeout_seconds=0.5, probe=reporting(ProcessStatus(True, 1)))
            errors = io.StringIO()
            with (
                mock.patch.object(review_io, "open_shared", side_effect=release_first),
                mock.patch.object(review_io.time, "sleep", side_effect=AssertionError("waited")),
                contextlib.redirect_stderr(errors),
                lock,
            ):
                self.assertEqual(lock.token, read_json(path / "owner.json")["token"])
            self.assertEqual([path / "owner.json"], opened)
            self.assertEqual("", errors.getvalue(), "a released lock is taken, not reclaimed")
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_torn_owner_file_is_kept_within_the_grace_period_and_reclaimed_after_it(self) -> None:
        cases = {
            "truncated": b'{"pid": 4242, "tok',
            "empty": b"",
            "not an object": b'["a"]',
            "no token": b'{"pid": 4242}',
            "empty token": b'{"pid": 4242, "token": ""}',
            "token an integer": b'{"pid": 4242, "token": 7}',
            "token an array": b'{"pid": 4242, "token": ["a"]}',
            "token an object": b'{"pid": 4242, "token": {}}',
            "token null": b'{"pid": 4242, "token": null}',
            "a string": b'"owner"',
            "null": b"null",
            "an integer": b"4242",
            "not UTF-8": b'{"token": "\xff"}',
            "oversized": b'{"token": "' + b"a" * (64 * 1024) + b'"}',
        }
        for name, content in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                path.mkdir()
                owner = path / "owner.json"
                owner.write_bytes(content)
                with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.1):
                    pass
                self.assertEqual(content, owner.read_bytes(), "a torn owner file is kept during the grace period")
                backdated = self._backdate(owner)
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5):
                    self.assertEqual(os.getpid(), read_json(owner)["pid"])
                self._assert_reclaimed_after(
                    errors.getvalue(),
                    path,
                    "its owner file has not been a valid owner record for {age} seconds",
                    backdated,
                )
                self.assertEqual([], list(Path(temporary).iterdir()))

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

    def test_atomic_json_gives_the_replacement_the_mode_it_is_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"answer": 42}, mode=0o644)
            self.assertEqual({"answer": 42}, read_json(path))
            if os.name != "nt":  # Windows keeps no permission bits but read-only
                self.assertEqual(0o644, path.stat().st_mode & 0o777)


class AtomicWriteTests(GitTestCase):
    @contextlib.contextmanager
    def watched_descriptors(self) -> Iterator[tuple[list[int], list[int], list[int]]]:
        """The descriptors atomic_write_text opens, each close of one, and each close that found its descriptor
        already closed. Such a close first gets an unrelated file at that number, as another thread's open would,
        so the close lands where a second close would land in a running prepare."""
        opened: list[int] = []
        closes: list[int] = []
        reused: list[int] = []
        real_mkstemp = tempfile.mkstemp
        real_close = os.close

        def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            descriptor, name = real_mkstemp(*args, **kwargs)
            opened.append(descriptor)
            return descriptor, name

        def close(descriptor: int) -> None:
            if descriptor in opened:
                closes.append(descriptor)
                try:
                    os.fstat(descriptor)
                except OSError:
                    unrelated = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(unrelated, descriptor)
                    real_close(unrelated)
                    reused.append(descriptor)
            real_close(descriptor)

        with mock.patch.object(review_io.tempfile, "mkstemp", mkstemp), mock.patch.object(review_io.os, "close", close):
            yield opened, closes, reused

    def test_each_failure_closes_the_descriptor_once_and_leaves_the_target(self) -> None:
        for name, failure in (
            ("chmod", mock.patch.object(Path, "chmod", side_effect=OSError("synthetic chmod"))),
            ("write", mock.patch.object(review_io.os, "write", side_effect=OSError("synthetic write"))),
            ("fsync", mock.patch.object(review_io.os, "fsync", side_effect=OSError("synthetic fsync"))),
            ("replace", mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace"))),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with (
                    self.watched_descriptors() as (opened, closes, reused),
                    failure,
                    self.assertRaisesRegex(PersistenceError, f"Cannot atomically replace .*synthetic {name}"),
                ):
                    review_io.atomic_write_text(path, "next\n")
                self.assertEqual(1, len(opened))
                self.assertEqual(opened, closes)
                self.assertEqual([], reused)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])
                self.assertEqual(b"previous\n", path.read_bytes())
                self.assertEqual(["state.json"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_success_closes_the_descriptor_once_and_writes_every_byte(self) -> None:
        real_write = os.write

        def short_write(descriptor: int, data: Any) -> int:
            return real_write(descriptor, bytes(data[:1]))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                self.watched_descriptors() as (opened, closes, reused),
                mock.patch.object(review_io.os, "write", short_write),
            ):
                review_io.atomic_write_text(path, "café\nnext\n")
            self.assertEqual(1, len(opened))
            self.assertEqual(opened, closes)
            self.assertEqual([], reused)
            self.assertEqual("café\nnext\n".encode(), path.read_bytes())
            self.assertEqual(["state.json"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_bytes_that_are_not_utf8_replace_the_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "condition.txt"
            path.write_bytes(b"previous\n")
            with self.watched_descriptors() as (opened, closes, _reused):
                review_io.atomic_write_bytes(path, b"caf\xc3\xa9 \xff\xc3")
            self.assertEqual(opened, closes)
            self.assertEqual(b"caf\xc3\xa9 \xff\xc3", path.read_bytes())
            self.assertEqual(["condition.txt"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_a_folder_that_cannot_be_made_or_text_utf8_cannot_encode_fails_before_a_copy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "blocker"
            blocker.write_bytes(b"a file where the folder would go\n")
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            for name, target, content, cause in (
                ("mkdir", blocker / "inner" / "state.json", "next\n", OSError),
                ("lone surrogate", path, "next \ud800\n", UnicodeEncodeError),
            ):
                with (
                    self.subTest(failure=name),
                    self.watched_descriptors() as (opened, _closes, _reused),
                    self.assertRaisesRegex(
                        PersistenceError, f"^Cannot atomically replace {re.escape(str(target))}: "
                    ) as raised,
                ):
                    review_io.atomic_write_text(target, content)
                self.assertIsInstance(raised.exception.__cause__, cause)
                self.assertEqual([], opened, "no copy was made")
            self.assertEqual(b"previous\n", path.read_bytes())
            self.assertEqual(["blocker", "state.json"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_a_copy_that_cannot_be_removed_is_named_in_a_persistence_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace")),
                mock.patch.object(Path, "unlink", side_effect=OSError("synthetic unlink")),
                self.assertRaises(PersistenceError) as raised,
            ):
                review_io.atomic_write_text(path, "next\n")
            copies = [item for item in Path(temporary).iterdir() if item.name != "state.json"]
            self.assertEqual(1, len(copies), "the copy stays where the error says")
            self.assertEqual(
                f"Cannot remove {copies[0]}, the unfinished copy of {path}: synthetic unlink", str(raised.exception)
            )
            removal = raised.exception.__cause__
            if not isinstance(removal, OSError):
                self.fail(f"Expected the removal's OSError, got {removal!r}")
            self.assertRegex(str(removal.__context__), "^Cannot atomically replace .*: synthetic replace$")
            self.assertEqual(b"previous\n", path.read_bytes())


class RecordTests(GitTestCase):
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
            if accepted:
                self.assertEqual(value, read_title(READ, value, "record.findings[0].title"))
        expected = "a non-empty trimmed single-line string of at most 120 characters"
        refused: tuple[tuple[Any, str], ...] = (
            (" padded", "a string with whitespace around it"),
            ("two\nlines", "a string of more than one line"),
            ("x" * 121, "a string of 121 characters"),
            (None, "null"),
            (7, "integer 7"),
            ("", "an empty string"),
        )
        for value, found in refused:
            with self.subTest(value=value):
                with self.assertRaises(RecordFieldError) as raised:
                    read_title(READ, value, "record.findings[0].title")
                self.assertEqual(f"record.findings[0].title must be {expected}, found {found}", str(raised.exception))

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
        title = "a non-empty trimmed single-line string of at most 120 characters"
        for bad, found in (
            ("", "an empty string"),
            ("x" * 121, "a string of 121 characters"),
            ("two\nlines", "a string of more than one line"),
            (7, "integer 7"),
        ):
            result = valid_adapter_result()
            result["findings"][0]["title"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern("adapter-result.findings[0].title", title, found)
            ):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["title"] = bad
            with self.assertRaisesRegex(RecordFieldError, field_pattern("record.findings[0].title", title, found)):
                validate_record(tampered)

    def test_a_finding_location_comes_from_the_suite_never_a_reviewer_and_is_validated_in_records(self) -> None:
        def validate(result: dict) -> dict:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        given = valid_adapter_result()
        given["findings"][0]["location"] = "paragraph 2"
        with self.assertRaisesRegex(
            RecordFieldError,
            field_pattern("adapter-result.findings[0]", "an object of known fields", 'unknown field "location"'),
        ):
            validate(given)
        result = validate(valid_adapter_result())
        result["findings"][0]["location"] = 'paragraph 2, under the heading "Scope"'
        record = build_record(valid_request(), result, version=1, policy={})
        self.assertEqual('paragraph 2, under the heading "Scope"', validate_record(record)["findings"][0]["location"])
        location = "a non-empty trimmed single-line string of at most 200 characters"
        for bad, found in (
            ("", "an empty string"),
            ("x" * 201, "a string of 201 characters"),
            ("two\nlines", "a string of more than one line"),
            (" padded", "a string with whitespace around it"),
            (7, "integer 7"),
        ):
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["location"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern("record.findings[0].location", location, found)
            ):
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
        analyzer_coverage = {"coverage": "available", "tool": "Microsoft.CodeAnalysis.NetAnalyzers", "rule": "CA2000"}
        covered["findings"][0]["analyzer"] = analyzer_coverage
        record = build_record(valid_request(), validate(covered), version=1, policy={})
        self.assertEqual(analyzer_coverage, validate_record(record)["findings"][0]["analyzer"])
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn(
            "> **Analyzer:** `CA2000` in `Microsoft.CodeAnalysis.NetAnalyzers`, which the repository "
            "already has, would catch this if enforced.\n",
            markdown,
        )
        name = "a name of at most 100 characters without whitespace, backticks, pipes, or angle brackets"
        for bad, field, expected, found in (
            ({"coverage": "known", "tool": "x"}, "", "an object with field rule", "an object without it"),
            ({"coverage": "custom-candidate", "tool": "Roslyn", "rule": "A B"}, ".rule", name, 'string "A B"'),
        ):
            result = valid_adapter_result()
            result["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern(f"adapter-result.findings[0].analyzer{field}", expected, found)
            ):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern(f"record.findings[0].analyzer{field}", expected, found)
            ):
                validate_record(tampered)
        unknown = valid_adapter_result()
        unknown["findings"][0]["note"] = "x"
        with self.assertRaisesRegex(
            RecordFieldError,
            field_pattern("adapter-result.findings[0]", "an object of known fields", 'unknown field "note"'),
        ):
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
        arguments: dict[str, Any] = {
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
            "| **Scope** | incremental&#44; 1 of 1 files and 3 of 3 changed lines differ from v1 &#40;requested "
            "auto&#58; a small change&#41; |\n",
            render_markdown(record, record_payload_hash="0" * 64),
        )
        uncompared = copy.deepcopy(record)
        uncompared["review"]["scope"].update(used="full", files_changed=None, lines_changed=None)
        validate_record(uncompared)
        self.assertIn("full&#44; could not compare with v1", render_markdown(uncompared, record_payload_hash="0" * 64))
        for mutate, message in (
            (lambda r: r["review"].update(patches={}), "patches must be a non-empty object"),
            (lambda r: r["review"].update(patches={"../x.cs": patches["src/file.cs"]}), "patch path is unsafe"),
            (
                lambda r: r["review"]["patches"]["src/file.cs"].update(sha256="C" * 64),
                field_pattern(
                    "record.review.patches.src/file.cs.sha256",
                    "a lowercase hexadecimal digest of 64 characters",
                    NOT_HEX,
                ),
            ),
            (
                lambda r: r["review"]["patches"]["src/file.cs"].update(lines=-1),
                field_pattern("record.review.patches.src/file.cs.lines", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"].update(mode="initial"), "Only a re-review has a scope"),
            (
                lambda r: r["review"]["scope"].update(requested="most"),
                field_pattern("record.review.scope.requested", 'one of "auto", "full", "incremental"', 'string "most"'),
            ),
            (
                lambda r: r["review"]["scope"].update(used="auto"),
                field_pattern("record.review.scope.used", 'one of "full", "incremental"', 'string "auto"'),
            ),
            (
                lambda r: r["review"]["scope"].update(reason=" "),
                field_pattern("record.review.scope.reason", "a non-empty string", "a blank string"),
            ),
            (lambda r: r["review"]["scope"].update(since_version=2), "since_version must be an earlier version"),
            (lambda r: r["review"]["scope"].update(since_version=0), "since_version must be an earlier version"),
            (
                lambda r: r["review"]["scope"].update(files_total=-1),
                field_pattern("record.review.scope.files_total", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"]["scope"].update(files_changed=2), "within their totals"),
            (lambda r: r["review"]["scope"].update(lines_changed=None), "both be null"),
            (
                lambda r: r["review"]["scope"].update(files_changed=None, lines_changed=None),
                "incremental re-review must have compared",
            ),
            (
                lambda r: r["review"]["scope"].pop("reason"),
                field_pattern("record.review.scope", "an object with field reason", "an object without it"),
            ),
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
            (
                lambda r: r.pop("comment_dispositions"),
                field_pattern("record.comment_dispositions", "an array", "null"),
            ),
            (lambda r: r.pop("github_comments"), "need the comments"),
            (lambda r: r["comment_dispositions"].clear(), r"missing=\['C1'\]"),
            (lambda r: r["github_comments"][0].update(id="X1"), "C<n>"),
            (
                lambda r: r["review"]["reviewers"][0].update(retries=-1),
                field_pattern("record.review.reviewers[0].retries", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"]["reviewers"].append(dict(r["review"]["reviewers"][0])), "unique"),
            (
                lambda r: r["review"].update(reviewers=[]),
                field_pattern("record.review.reviewers", "a non-empty array", "an empty array"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=-1),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "integer -1"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=1.5),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "number 1.5"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=True),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "boolean true"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(minutes=1),
                field_pattern("record.review.reviewers[0]", "an object of known fields", 'unknown field "minutes"'),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model=""),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "an empty string"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model="a\nb"),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "a string of more than one line"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model=5),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "integer 5"),
            ),
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
        self.assertLess(markdown.index("Addressed in v2: Fixed&#46;"), markdown.index("## Review Comments"))
        self.assertIn(
            "| [C1](https://example.invalid/1) | &#64;dev on `src/file.cs`: Why&#63; "
            "| STILL PRESENT | Unchanged&#46; |",
            markdown,
        )
        self.assertIn("## Reviewers", markdown)
        self.assertLess(markdown.index("## Review Comments"), markdown.index("## Reviewers"))
        self.assertLess(markdown.index("## Reviewers"), markdown.index("Review Details</strong>"))
        self.assertIn("| STILL PRESENT | Unchanged&#46; |\n\n**0/1 addressed**\n", markdown)
        self.assertEqual(1, markdown.count(" addressed**"), "the prior findings' count is in the verdict row")

    def test_unhashable_values_are_validation_errors_not_crashes(self) -> None:
        arguments: dict[str, Any] = {
            "expected_repository": "example/one",
            "expected_number": 12,
            "expected_head_sha": "b" * 40,
        }
        for mutate, message in (
            (
                lambda r: r.update(status=["complete"]),
                field_pattern("adapter-result.status", 'one of "complete", "partial", "failed"', "an array"),
            ),
            (
                lambda r: r["findings"][0].update(severity=["MUST_FIX"]),
                field_pattern("adapter-result.findings[0].severity", SEVERITIES, "an array"),
            ),
            (
                lambda r: r.update(
                    prior_dispositions=[{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
                ),
                field_pattern("adapter-result.prior_dispositions[0].disposition", DISPOSITIONS, "an array"),
            ),
            (
                lambda r: r.update(
                    comment_dispositions=[{"comment_id": ["C1"], "disposition": "addressed", "rationale": "x"}]
                ),
                field_pattern("adapter-result.comment_dispositions[0].comment_id", "a non-empty string", "an array"),
            ),
        ):
            result = valid_adapter_result()
            result["prior_dispositions"] = [{"finding_id": "F001", "disposition": "addressed", "rationale": "x"}]
            mutate(result)
            with self.subTest(message=message), self.assertRaisesRegex(RecordFieldError, message):
                validate_adapter_result(result, prior_ids=["F001"], comment_ids=["C1"], **arguments)
        record = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        for field, value, expected, found in (
            ("verdict", ["APPROVED"], 'one of "APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"', "an array"),
            ("mode", {"initial": 1}, 'one of "initial", "re-review"', "an object"),
        ):
            broken = copy.deepcopy(record)
            broken["review"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RecordFieldError, field_pattern(f"record.review.{field}", expected, found)),
            ):
                validate_record(broken)
        broken = copy.deepcopy(record)
        broken["prior_dispositions"] = [{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
        with self.assertRaisesRegex(
            RecordFieldError, field_pattern("record.prior_dispositions[0].disposition", DISPOSITIONS, "an array")
        ):
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

    def test_a_record_pair_without_artifact_hashes_is_refused(self) -> None:
        # The contract reads a null `artifacts` as absent, and an archived pair without them cannot be verified.
        record = build_record(
            valid_request(),
            validate_adapter_result(
                valid_adapter_result(),
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            ),
            version=1,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            reviewed_at="2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(record_artifacts(record))
        for case in ("null", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                json_path = Path(temporary) / "review.json"
                markdown_path = Path(temporary) / "review.md"
                persisted = write_record_pair(json_path, markdown_path, record)
                self.assertEqual(persisted["artifacts"], record_artifacts(persisted))
                stored = json.loads(json_path.read_text(encoding="utf-8"))
                if case == "null":
                    stored["artifacts"] = None
                else:
                    del stored["artifacts"]
                json_path.write_text(json.dumps(stored), encoding="utf-8")
                with self.assertRaisesRegex(RecordError, "^Review record has no artifact hashes$"):
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
            "| **Title** | Fix &#124; split |",
            "| **Reviewed** | 01-Oct-2026 21:26 UTC |",
            "| **Verdict** | CHANGES REQUESTED, 3 open since v2 |",
            "<summary><strong>MUST FIX (1)</strong></summary>",
            "<summary>v3 F001. [A&lt;B&gt;] List&lt;T&gt; &amp; friends leak</summary>",
            "<summary>v3 F002. [Correctness] <code>z&#46;cs:9</code></summary>",
            "> **File:** `src/a.cs`  \n> **Line:** 3 | **Source:** generic\n>\n"
            "> First paragraph&#46;\n>\n> Second paragraph&#46;\n>\n> **Evidence:** ``a.cs:3 adds: f(`x`) | y``\n",
            "<summary><strong>SHOULD FIX (1)</strong></summary>",
            "> **Open since v2.** Partially addressed in v3: One &#124; of two&#46;\n",
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
        with self.assertRaisesRegex(
            RecordFieldError, field_pattern("record.pull_request.head_ref", "a non-empty string", "a blank string")
        ):
            validate_record(broken)

    def test_the_record_keeps_how_much_of_the_description_reviewers_were_given_and_the_report_says_so(self) -> None:
        rows = {
            (0, 0): "| **Description** | none |",
            (1234, 1234): "| **Description** | 1,234 characters, given to reviewers whole |",
            (70_000, 24_000): "| **Description** | 70,000 characters, of which reviewers were given the first 24,000 |",
        }
        for (characters, given), row in rows.items():
            with self.subTest(characters=characters):
                request = {**valid_request(), "body_characters": characters, "body_given": given}
                record = build_record(request, valid_adapter_result(), version=1, policy={})
                validate_record(record)
                self.assertEqual(
                    (characters, given),
                    (record["pull_request"]["body_characters"], record["pull_request"]["body_given"]),
                )
                markdown = render_markdown(record, record_payload_hash="0" * 64)
                self.assertEqual(
                    [row], [line for line in markdown.splitlines() if line.startswith("| **Description**")]
                )
        older = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertNotIn("body_characters", older["pull_request"])
        self.assertNotIn("**Description**", render_markdown(older, record_payload_hash="0" * 64))
        faults: list[tuple[Any, Any]] = [(5, 6), (-1, 0), (True, 0), (5, "5")]
        for characters, given in faults:
            with self.subTest(characters=characters, given=given):
                broken = copy.deepcopy(older)
                broken["pull_request"].update(body_characters=characters, body_given=given)
                with self.assertRaisesRegex(RecordError, "body_"):
                    validate_record(broken)


class ArchiveTests(GitTestCase):
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
            latest = latest_record(root, "example/one", 12)
            if latest is None:
                self.fail("the second version is archived")
            self.assertEqual(2, latest["review"]["version"])

    def test_versions_list_every_number_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            names = ["review.json", *(f"review-v{n}.json" for n in range(2, 12)), "review-v100.json"]
            # Not record names: version 1 is review.json, and versions carry no leading zeros.
            names += ["review-v1.json", "review-v0.json", "review-v01.json", "review-v010.json", "review-vx.json"]
            for name in names:
                (directory / name).write_text("{}", encoding="utf-8")
            self.assertEqual([*range(1, 12), 100], list_versions(directory))

    def test_record_files_are_every_pair_of_a_repository_beside_its_version_chains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual([], record_files(root, "example/one"), "a repository with no reviews has none")
            pulls = pull_directory(root, "Example/One", 7).parent
            for name in ("7/review.json", "7/review-v2.json", "7/review-imported.json", "12/review.json"):
                (pulls / name).parent.mkdir(parents=True, exist_ok=True)
                (pulls / name).write_text("{}", encoding="utf-8")
            (pulls / "7" / "review.md").write_text("", encoding="utf-8")
            (pulls / "7" / "notes.json").write_text("{}", encoding="utf-8")
            other = pull_directory(root, "example/other", 7)
            other.mkdir(parents=True)
            (other / "review.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                [
                    pulls / "12/review.json",
                    pulls / "7/review-imported.json",
                    pulls / "7/review-v2.json",
                    pulls / "7/review.json",
                ],
                record_files(root, "EXAMPLE/one"),
            )
            self.assertEqual([1, 2], list_versions(pulls / "7"), "the chain leaves the imported pair out")

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
            latest = latest_record(root, "example/one", 12)
            if latest is None:
                self.fail("eleven versions are archived")
            self.assertEqual(11, latest["review"]["version"])


class FlagTests(GitTestCase):
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


class GitHubTests(GitTestCase):
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
                "github_client.run_bounded",
                side_effect=FileNotFoundError("gh was not found"),
            ),
            self.assertRaises(GitHubError) as context,
        ):
            review_github.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertIn("install GitHub CLI", str(context.exception))

    def test_pagination_is_flattened(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(
                0,
                json.dumps([[self._api_pull(1)], [self._api_pull(2)]]),
                "",
            )

        listing = GitHubClient(runner).list_pulls("example/one", state="open")
        self.assertEqual([1, 2], [item["number"] for item in listing.pulls])
        self.assertEqual(2, listing.pages)
        self.assertIn("--paginate", calls[0])
        self.assertIn("--slurp", calls[0])

    @staticmethod
    def _closed_page(numbers: range | list[int], updated: str, *, merged: str | None = None) -> list[dict]:
        pulls = []
        for number in numbers:
            value = GitHubTests._api_pull(number, state="closed", merged_at=merged or updated)
            value["updated_at"] = updated
            pulls.append(value)
        return pulls

    def _walk(self, pages: dict[int, Any], since: date) -> tuple[review_github.PullListing, list[list[str]]]:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            page = int(arguments[-1].rsplit("&page=", 1)[1])
            return CommandResult(0, json.dumps(pages.get(page, [])), "")

        return GitHubClient(runner).list_closed_pulls_since("example/one", since), calls

    def test_closed_walk_stops_at_the_first_page_behind_the_watermark(self) -> None:
        pages = {
            1: self._closed_page(range(300, 200, -1), "2026-03-09T10:00:00Z"),
            2: self._closed_page(range(200, 100, -1), "2026-02-27T10:00:00Z"),
            3: self._closed_page(range(100, 0, -1), "2026-01-01T10:00:00Z"),
        }
        listing, calls = self._walk(pages, date(2026, 3, 1))
        # Page 2 ends before the watermark, so page 3 is never read; page 1 is read again after a walk of two pages.
        self.assertEqual(
            [
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=2",
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
            ],
            [call[-1] for call in calls],
        )
        self.assertTrue(all("--paginate" not in call for call in calls))
        self.assertEqual(3, listing.pages)
        self.assertEqual(list(range(300, 100, -1)), [pull["number"] for pull in listing.pulls])

    def test_closed_walk_reads_one_page_when_it_reaches_the_watermark_or_the_end(self) -> None:
        cases: dict[str, dict[int, Any]] = {
            "behind the watermark": {1: self._closed_page(range(100, 0, -1), "2026-02-01T10:00:00Z")},
            "short page": {1: self._closed_page(range(5, 0, -1), "2026-03-05T10:00:00Z")},
            "no closed pull": {1: []},
        }
        for name, pages in cases.items():
            with self.subTest(name):
                listing, calls = self._walk(pages, date(2026, 3, 1))
                self.assertEqual(1, len(calls))
                self.assertEqual(1, listing.pages)

    def test_closed_walk_keeps_a_page_that_ends_on_the_watermark_day(self) -> None:
        pages = {
            1: self._closed_page(range(200, 100, -1), "2026-03-01T00:00:00Z"),
            2: self._closed_page(range(100, 98, -1), "2026-02-28T23:59:59Z"),
        }
        listing, calls = self._walk(pages, date(2026, 3, 1))
        self.assertEqual(["page=1", "page=2", "page=1"], [call[-1].rsplit("&", 1)[1] for call in calls])
        self.assertEqual(102, len(listing.pulls))

    def test_closed_walk_reads_update_times_as_utc_dates(self) -> None:
        # The first page ends just before the watermark's UTC day, though its local date east of UTC is already that
        # day, so the walk stops; one that ends just after it, on the previous local day west of UTC, reads on.
        for updated, reads in (("2026-03-01T01:00:00+02:00", 1), ("2026-02-28T20:00:00-05:00", 3)):
            with self.subTest(updated=updated):
                pages = {
                    1: self._closed_page(range(200, 100, -1), updated),
                    2: self._closed_page(range(100, 98, -1), "2026-02-20T10:00:00Z"),
                }
                _, calls = self._walk(pages, date(2026, 3, 1))
                self.assertEqual(reads, len(calls))

    def test_closed_walk_finds_a_pull_updated_onto_a_page_it_already_read(self) -> None:
        # Pull 50 is merged while page 1 is being read, so it moves from page 2 to the top of page 1, and page 1's last
        # pull shifts onto page 2: the second page repeats it, and the reread of page 1 finds pull 50.
        first = self._closed_page(range(300, 200, -1), "2026-03-09T10:00:00Z")
        moved = self._closed_page([50], "2026-03-10T10:00:00Z")
        reads = {1: 0}
        calls: list[str] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(arguments[-1])
            page = int(arguments[-1].rsplit("&page=", 1)[1])
            if page == 1:
                reads[1] += 1
                return CommandResult(0, json.dumps(first if reads[1] == 1 else moved + first[:99]), "")
            shifted = first[99:] + self._closed_page(range(200, 101, -1), "2026-02-27T10:00:00Z")
            return CommandResult(0, json.dumps(shifted if page == 2 else []), "")

        listing = GitHubClient(runner).list_closed_pulls_since("example/one", date(2026, 3, 1))
        numbers = [pull["number"] for pull in listing.pulls]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertIn(50, numbers)
        self.assertEqual(3, listing.pages)

    def test_closed_walk_drops_closed_unmerged_pulls_but_pages_past_them(self) -> None:
        page = self._closed_page(range(200, 100, -1), "2026-03-05T10:00:00Z")
        for value in page[:50]:
            value["merged_at"] = None
        listing, calls = self._walk({1: page}, date(2026, 3, 1))
        self.assertEqual(3, len(calls))  # a full page that reaches the watermark is followed by page 2
        self.assertEqual(list(range(150, 100, -1)), [pull["number"] for pull in listing.pulls])
        self.assertTrue(all(pull["state"] == "MERGED" for pull in listing.pulls))

    def test_closed_walk_fails_on_a_page_it_cannot_read(self) -> None:
        for name, page in {
            "not a list": {"message": "unexpected"},
            "no update time": [
                {k: v for k, v in p.items() if k != "updated_at"}
                for p in self._closed_page(range(100, 0, -1), "2026-03-05T10:00:00Z")
            ],
            "bad update time": self._closed_page(range(100, 0, -1), "yesterday"),
        }.items():
            with self.subTest(name), self.assertRaises(GitHubError) as context:
                self._walk({1: page}, date(2026, 3, 1))
            self.assertEqual("malformed", context.exception.kind)

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

        def runner(arguments: Sequence[str]) -> CommandResult:
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
                field_pattern("graphql response.data.repository.pullRequests.pageInfo", "an object", "null"),
            ),
            (CommandResult(0, self._graphql_page([1], "same"), ""), "malformed", "did not advance"),
            (CommandResult(1, "", "HTTP 401: Bad credentials"), "authentication", "Bad credentials"),
            (CommandResult(0, "not-json", ""), "malformed", "malformed JSON"),
        )
        for response, kind, message in cases:
            with self.subTest(kind=kind, message=message):
                waits: list[float] = []
                client = GitHubClient(answering(response), sleeper=waits.append)
                with self.assertRaisesRegex(GitHubError, message) as context:
                    client.graphql_nodes("query", {}, connection)
                self.assertEqual(kind, context.exception.kind)
                self.assertEqual(5 if kind == "rate_limit" else 0, len(waits))

    def test_authenticated_login_is_shape_checked(self) -> None:
        self.assertEqual(
            "octo",
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({"login": "octo"}), "")).authenticated_login(),
        )
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("user.login", "a non-empty string", "null")):
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({}), "")).authenticated_login()

    def test_single_pull_fetch_is_explicit_and_shape_checked(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(0, json.dumps(self._api_pull(42)), "")

        pull = GitHubClient(runner).get_pull("Example/One", 42)
        self.assertEqual(42, pull["number"])
        self.assertEqual("repos/example/one/pulls/42", calls[0][-1])
        self.assertNotIn("--paginate", calls[0])
        with self.assertRaisesRegex(
            GitHubInputError, field_pattern("pull number", "an integer of at least 1", "integer 0")
        ) as refused:
            GitHubClient(runner).get_pull("example/one", 0)
        self.assertEqual("input", refused.exception.kind)
        malformed = GitHubClient(lambda arguments: CommandResult(0, json.dumps([]), ""))
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("pull", "an object", "an array")) as context:
            malformed.get_pull("example/one", 42)
        self.assertEqual("malformed", context.exception.kind)

    def test_the_pull_a_review_reads_carries_its_description_and_a_null_one_is_empty(self) -> None:
        for body, expected in (("## Upgrade note\n\nNone\n", "## Upgrade note\n\nNone\n"), (None, ""), ("", "")):
            with self.subTest(body=body):
                value = {**self._api_pull(42), "body": body}
                served = CommandResult(0, json.dumps(value), "")

                def serve(arguments: Sequence[str], served: CommandResult = served) -> CommandResult:
                    return served

                client = GitHubClient(serve)
                pull = client.get_pull("example/one", 42)
                self.assertEqual(expected, pull["body"])
                self.assertEqual(pull, validate_pull(pull))
        value = {**self._api_pull(42), "body": ["not", "text"]}
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("pull.body", "a string or null", "an array")):
            GitHubClient(lambda arguments: CommandResult(0, json.dumps(value), "")).get_pull("example/one", 42)
        with self.assertRaisesRegex(ReviewOperationFieldError, field_pattern("pull.body", "a string", "null")):
            validate_pull({**pull, "body": None})

    def test_a_response_field_of_the_wrong_kind_is_a_malformed_response(self) -> None:
        pull = self._api_pull(42)
        page = json.loads(self._graphql_page([1], "c1"))
        connection = "graphql response.data.repository.pullRequests"

        def with_page(**fields: Any) -> dict:
            changed = copy.deepcopy(page)
            changed["data"]["repository"]["pullRequests"].update(fields)
            return changed

        cases: tuple[tuple[str, Any, str, str, str], ...] = (
            ("pull", [], "pull", "an object", "an array"),
            ("pull", {**pull, "state": 1}, "pull.state", 'one of "open", "closed"', "integer 1"),
            ("pull", {**pull, "state": "OPEN"}, "pull.state", 'one of "open", "closed"', 'string "OPEN"'),
            ("pull", {**pull, "merged_at": 5}, "pull.merged_at", "a non-empty string or null", "integer 5"),
            ("pull", {**pull, "merged_at": ""}, "pull.merged_at", "a non-empty string or null", "an empty string"),
            ("pull", {**pull, "base": "main"}, "pull.base", "an object", 'string "main"'),
            ("pull", {**pull, "head": []}, "pull.head", "an object", "an array"),
            ("pull", {**pull, "body": 7}, "pull.body", "a string or null", "integer 7"),
            ("pull", {**pull, "body": {}}, "pull.body", "a string or null", "an object"),
            ("user", [], "user", "an object", "an array"),
            ("user", {"login": 5}, "user.login", "a non-empty string", "integer 5"),
            ("user", {"login": ""}, "user.login", "a non-empty string", "an empty string"),
            ("graphql", [], "graphql response", "an object", "an array"),
            ("graphql", {"data": []}, "graphql response.data", "an object", "an array"),
            ("graphql", {"data": {"repository": []}}, "graphql response.data.repository", "an object", "an array"),
            (
                "graphql",
                {"data": {"repository": {"pullRequests": "x"}}},
                connection,
                "an object",
                'string "x"',
            ),
            ("graphql", with_page(nodes="x"), f"{connection}.nodes", "an array", 'string "x"'),
            ("graphql", with_page(nodes=[1]), f"{connection}.nodes[0]", "an object", "integer 1"),
            ("graphql", with_page(pageInfo=[]), f"{connection}.pageInfo", "an object", "an array"),
            (
                "graphql",
                with_page(pageInfo={"hasNextPage": "yes", "endCursor": None}),
                f"{connection}.pageInfo.hasNextPage",
                "a boolean",
                'string "yes"',
            ),
            (
                "graphql",
                with_page(pageInfo={"hasNextPage": True, "endCursor": 5}),
                f"{connection}.pageInfo.endCursor",
                "a non-empty string",
                "integer 5",
            ),
        )
        for read, value, path, expected, found in cases:
            with self.subTest(read=read, path=path, found=found):
                client = GitHubClient(answering(CommandResult(0, json.dumps(value), "")))
                with self.assertRaises(GitHubFieldError) as context:
                    if read == "pull":
                        client.get_pull("example/one", 42)
                    elif read == "user":
                        client.authenticated_login()
                    else:
                        client.graphql_nodes("query", {}, ("repository", "pullRequests"))
                self.assertNotIsInstance(context.exception, GitHubInputError)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)

    def test_an_argument_of_the_wrong_kind_fails_before_github_is_asked(self) -> None:
        number = "an integer of at least 1"
        # Each read, the arguments it is given, and the argument of the wrong kind it names.
        cases: tuple[tuple[str, tuple[Any, ...], dict[str, Any], str, str, str], ...] = (
            ("get_pull", ("example/one", "42"), {}, "pull number", number, 'string "42"'),
            ("get_pull", ("example/one", True), {}, "pull number", number, "boolean true"),
            ("get_pull", ("example/one", 42.0), {}, "pull number", number, "number 42.0"),
            ("get_pull_diff", ("example/one", -1), {}, "pull number", number, "integer -1"),
            ("list_open_review_threads", ("example/one", 0), {}, "pull number", number, "integer 0"),
            (
                "list_pulls",
                ("example/one",),
                {"state": "merged"},
                "pull state",
                'one of "open", "closed", "all"',
                'string "merged"',
            ),
            ("get_merge_base", ("example/one", 1, "b" * 40), {}, "base commit", OBJECT_ID, "integer 1"),
            ("get_file_blob", ("example/one", None, "a.txt"), {"maximum": 1}, "commit", OBJECT_ID, "null"),
        )
        for name, arguments, options, path, expected, found in cases:
            with self.subTest(read=name, path=path, found=found):
                calls: list[list[str]] = []

                def runner(arguments: Sequence[str], calls: list[list[str]] = calls) -> CommandResult:
                    calls.append(list(arguments))
                    return CommandResult(0, "{}", "")

                with self.assertRaises(GitHubInputError) as context:
                    getattr(GitHubClient(runner), name)(*arguments, **options)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
                self.assertEqual("input", context.exception.kind)
                self.assertEqual([], calls)

    def test_closed_unmerged_pulls_are_ineligible(self) -> None:
        closed = self._api_pull(42, state="closed")
        client = GitHubClient(lambda arguments: CommandResult(0, json.dumps(closed), ""))
        with self.assertRaisesRegex(GitHubError, "closed without being merged"):
            client.get_pull("example/one", 42)

    def test_api_errors_fail_closed_and_expected_404_can_be_absent(self) -> None:
        def runner(arguments: Sequence[str]) -> CommandResult:
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
        limited = [5.0, 10.0, 20.0, 40.0, 80.0]
        cases: tuple[tuple[str, str, list[float]], ...] = (
            ("API rate limit exceeded", "rate_limit", limited),
            ("HTTP 429: too many requests", "rate_limit", limited),
            ("HTTP 401: authentication required", "authentication", []),
            ("not logged into GitHub", "authentication", []),
            ("HTTP 403: forbidden", "forbidden", []),
            ("HTTP 404: not found", "not_found", []),
            ("connection refused", "network", []),
            ("connection reset", "network", [2.0, 4.0]),
            ("HTTP 502: Bad Gateway", "api", [2.0, 4.0]),
            ("HTTP 500: Internal Server Error", "api", []),
        )
        for stderr, expected, retried in cases:
            with self.subTest(stderr=stderr):
                waits: list[float] = []
                client = GitHubClient(answering(CommandResult(1, "", stderr)), sleeper=waits.append)
                with self.assertRaises(GitHubError) as context:
                    client.api_json("repos/example/one")
                self.assertEqual(expected, context.exception.kind)
                # skill-core's bounded backoff waits out a rate limit, and its short retries a transient failure on
                # this read, before it fails; nothing else is waited out.
                self.assertEqual(retried, waits)

    def test_a_rate_limit_is_retried_until_it_clears(self) -> None:
        answers = [CommandResult(1, "", "gh: API rate limit exceeded (HTTP 403)"), CommandResult(0, "[]", "")]
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return answers.pop(0)

        waits: list[float] = []
        self.assertEqual([], GitHubClient(runner, sleeper=waits.append).api_json("repos/example/one/pulls"))
        self.assertEqual([5.0], waits)
        self.assertEqual([["gh", "api", "repos/example/one/pulls"]] * 2, calls)

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
                client = GitHubClient(answering(CommandResult(0, value, "")))
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


class GitHubDocumentTests(GitTestCase):
    @staticmethod
    def _client(*values: Any) -> tuple[GitHubClient, list[list[str]]]:
        responses = [CommandResult(0, json.dumps(value), "") for value in values]
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return responses[len(calls) - 1]

        return GitHubClient(runner), calls

    def test_merge_base_uses_the_compare_endpoint_and_accepts_both_object_id_lengths(self) -> None:
        for base, head, sha in (("a" * 40, "b" * 64, "c" * 40), ("a" * 64, "b" * 40, "c" * 64)):
            with self.subTest(base=base, head=head, sha=sha):
                client, calls = self._client({"merge_base_commit": {"sha": sha}})
                self.assertEqual(sha, client.get_merge_base("Example/One", base, head))
                self.assertEqual([["gh", "api", f"repos/example/one/compare/{base}...{head}?per_page=1"]], calls)
                self.assertEqual(1, client.call_count)

    def test_merge_base_rejects_invalid_commit_ids_before_running_gh(self) -> None:
        for invalid, found in INVALID_COMMITS:
            for base, head, which in ((invalid, "b" * 40, "base"), ("a" * 40, invalid, "head")):
                with self.subTest(base=base, head=head):
                    client, calls = self._client()
                    with self.assertRaises(GitHubInputError) as context:
                        client.get_merge_base("example/one", base, head)
                    self.assertEqual(field_message(f"{which} commit", OBJECT_ID, found), str(context.exception))
                    self.assertEqual("input", context.exception.kind)
                    self.assertEqual([], calls)
                    self.assertEqual(0, client.call_count)

    def test_merge_base_rejects_malformed_compare_responses(self) -> None:
        sha = "compare.merge_base_commit.sha"
        values: tuple[tuple[Any, str], ...] = (
            ([], "compare must be an object, found an array"),
            ({}, "compare.merge_base_commit must be an object, found null"),
            ({"merge_base_commit": []}, "compare.merge_base_commit must be an object, found an array"),
            ({"merge_base_commit": {}}, field_message(sha, OBJECT_ID, "null")),
            ({"merge_base_commit": {"sha": 1}}, field_message(sha, OBJECT_ID, "integer 1")),
            ({"merge_base_commit": {"sha": "a" * 39}}, field_message(sha, OBJECT_ID, "a string of 39 characters")),
            ({"merge_base_commit": {"sha": "g" * 40}}, field_message(sha, OBJECT_ID, NOT_HEX)),
        )
        for value, message in values:
            with self.subTest(value=value):
                client, calls = self._client(value)
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_merge_base("example/one", "a" * 40, "b" * 40)
                self.assertNotIsInstance(context.exception, GitHubInputError)
                self.assertEqual(message, str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_invalid_commit_ids_before_running_gh(self) -> None:
        for commit, found in INVALID_COMMITS:
            with self.subTest(commit=commit):
                client, calls = self._client()
                with self.assertRaises(GitHubInputError) as context:
                    client.get_file_blob("example/one", commit, "docs/design.docx", maximum=1)
                self.assertEqual(field_message("commit", OBJECT_ID, found), str(context.exception))
                self.assertEqual("input", context.exception.kind)
                self.assertEqual([], calls)
                self.assertEqual(0, client.call_count)

    def test_file_blob_rejects_contents_that_are_not_one_file(self) -> None:
        with self.subTest(value=[]):
            client, calls = self._client([])
            with self.assertRaisesRegex(GitHubFieldError, "^contents must be an object, found an array$") as field:
                client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
            self.assertEqual("malformed", field.exception.kind)
            self.assertEqual(1, len(calls))
        values: tuple[Any, ...] = ({}, {"type": "dir"}, {"type": 1}, {"type": "Symlink"})
        for value in values:
            with self.subTest(value=value):
                client, calls = self._client(value)
                with self.assertRaisesRegex(GitHubError, "^Contents response is not one file$") as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertNotIsInstance(context.exception, review_github.NotOneFile)
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_names_a_symbolic_link_or_a_submodule_and_reads_no_blob(self) -> None:
        # GitHub answers a link whose target is not a file in the repository, and a submodule, with their own type.
        for value, entry in (
            ({"type": "symlink", "target": "../../outside.md"}, "symbolic link"),
            ({"type": "submodule", "submodule_git_url": "https://example.invalid/x.git"}, "submodule"),
        ):
            with self.subTest(entry=entry):
                client, calls = self._client(value)
                with self.assertRaises(review_github.NotOneFile) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/link.md", maximum=1)
                self.assertEqual(
                    f"Contents response is not one file: docs/link.md is a {entry}", str(context.exception)
                )
                self.assertEqual((entry, "not_file"), (context.exception.entry, context.exception.kind))
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_contents_with_invalid_blob_ids(self) -> None:
        for fields, found in (
            ({}, "null"),
            ({"sha": 1}, "integer 1"),
            ({"sha": "b" * 39}, "a string of 39 characters"),
            ({"sha": "g" * 40}, NOT_HEX),
            ({"sha": "B" * 64}, NOT_HEX),
        ):
            with self.subTest(fields=fields):
                client, calls = self._client({"type": "file", "size": 0, **fields})
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(field_message("contents.sha", OBJECT_ID, found), str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_contents_without_a_nonnegative_integer_size(self) -> None:
        for fields, found in (
            ({}, "null"),
            ({"size": None}, "null"),
            ({"size": "1"}, 'string "1"'),
            ({"size": 1.5}, "number 1.5"),
            ({"size": True}, "boolean true"),
            ({"size": -1}, "integer -1"),
        ):
            with self.subTest(fields=fields):
                client, calls = self._client({"type": "file", "sha": "b" * 40, **fields})
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(
                    field_message("contents.size", "an integer of at least 0", found), str(context.exception)
                )
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_fetches_valid_bytes_below_and_at_the_size_limit_with_quoted_path(self) -> None:
        payload = b"\x00\xff\n"
        for commit, sha, maximum in (("a" * 40, "b" * 64, 4), ("a" * 64, "b" * 40, 3)):
            with self.subTest(commit=commit, sha=sha, maximum=maximum):
                client, calls = self._client(
                    {"type": "file", "sha": sha, "size": len(payload)},
                    {"encoding": "base64", "content": "AP8K\n"},
                )
                self.assertEqual(
                    (sha, payload),
                    client.get_file_blob("Example/One", commit, "docs/A design #1?+%.docx", maximum=maximum),
                )
                self.assertEqual(
                    [
                        ["gh", "api", f"repos/example/one/contents/docs/A%20design%20%231%3F%2B%25.docx?ref={commit}"],
                        ["gh", "api", f"repos/example/one/git/blobs/{sha}"],
                    ],
                    calls,
                )
                self.assertEqual(2, client.call_count)

    def test_file_blob_fetches_an_empty_file_at_a_zero_size_limit(self) -> None:
        sha = "b" * 40
        client, calls = self._client({"type": "file", "sha": sha, "size": 0}, {"encoding": "base64", "content": ""})
        self.assertEqual((sha, b""), client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=0))
        self.assertEqual(2, len(calls))

    def test_file_blob_returns_the_id_without_fetching_content_one_byte_over_the_limit(self) -> None:
        sha, commit = "b" * 40, "a" * 40
        client, calls = self._client({"type": "file", "sha": sha, "size": 4})
        self.assertEqual((sha, None), client.get_file_blob("example/one", commit, "docs/design.docx", maximum=3))
        self.assertEqual([["gh", "api", f"repos/example/one/contents/docs/design.docx?ref={commit}"]], calls)
        self.assertEqual(1, client.call_count)

    def test_file_blob_rejects_malformed_blob_responses(self) -> None:
        values: tuple[tuple[Any, str], ...] = (
            ([], "blob must be an object, found an array"),
            ({}, "Blob response has an unexpected shape"),
            ({"encoding": "utf-8", "content": "text"}, "Blob response has an unexpected shape"),
            ({"encoding": "base64"}, "blob.content must be a string, found null"),
            ({"encoding": "base64", "content": 1}, "blob.content must be a string, found integer 1"),
        )
        for value, message in values:
            with self.subTest(value=value):
                client, calls = self._client({"type": "file", "sha": "b" * 40, "size": 1}, value)
                with self.assertRaises(GitHubError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(message, str(context.exception))
                self.assertEqual(" must be " in message, isinstance(context.exception, GitHubFieldError))
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(2, len(calls))

    def test_file_blob_classifies_base64_decode_failures(self) -> None:
        for content in ("a", "\u00e9"):
            with self.subTest(content=content):
                client, calls = self._client(
                    {"type": "file", "sha": "b" * 40, "size": 1}, {"encoding": "base64", "content": content}
                )
                with self.assertRaisesRegex(GitHubError, "^Blob response is not base64$") as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)
                self.assertIsNotNone(context.exception.__cause__)
                self.assertEqual(2, len(calls))

    def test_document_reads_propagate_gh_and_json_failures_without_fetching_further(self) -> None:
        contents = CommandResult(0, json.dumps({"type": "file", "sha": "b" * 40, "size": 1}), "")
        for failure, kind, message in (
            (CommandResult(1, "", "HTTP 404: Not Found"), "not_found", "^HTTP 404: Not Found$"),
            (CommandResult(0, "not-json", ""), "malformed", "^GitHub returned malformed JSON:"),
        ):
            for stage in ("compare", "contents", "blob"):
                with self.subTest(stage=stage, kind=kind):
                    responses = [contents, failure] if stage == "blob" else [failure]
                    runner = mock.Mock(side_effect=responses)
                    client = GitHubClient(runner)
                    with self.assertRaisesRegex(GitHubError, message) as context:
                        if stage == "compare":
                            client.get_merge_base("example/one", "a" * 40, "b" * 40)
                        else:
                            client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                    self.assertEqual(kind, context.exception.kind)
                    self.assertFalse(context.exception.retryable)
                    self.assertEqual(1 if kind == "not_found" else None, context.exception.returncode)
                    self.assertEqual(len(responses), runner.call_count)
                    self.assertEqual(len(responses), client.call_count)


class RuntimeContractTests(GitTestCase):
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
            target.write_bytes(content.encode("utf-8"))
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
        with self.assertRaisesRegex(
            RuntimeContractFieldError,
            field_pattern("entrypoint-manifest.supports[0]", 'one of "initial", "re-review"', "an array"),
        ):
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

    def test_an_inline_review_offers_only_reading_the_diff_and_writing_the_result(self) -> None:
        # Literal capability names: what an inline reviewer can do is part of the manifest contract.
        self.assertEqual(
            {"read-diff", "write-result"}, negotiate_capabilities("claude-code", ["read-diff"], dispatch="inline")
        )
        self.assertEqual(
            {"read-diff", "write-result"}, negotiate_capabilities("copilot-cli", ["write-result"], dispatch="inline")
        )
        cases = (
            ("claude-code", ["agent-delegation"], "Inline review lacks required capabilities: agent-delegation"),
            ("codex", ["agent-delegation", "read-diff"], "Inline review lacks required capabilities: agent-delegation"),
            # The runtime's own lack is named first, as it is for every other dispatch.
            ("copilot-cli", ["agent-delegation"], "Runtime copilot-cli lacks required capabilities: agent-delegation"),
            (
                "copilot-cli",
                ["isolated-added-root"],
                "Inline review lacks required capabilities: isolated-added-root",
            ),
        )
        for runtime, required, message in cases:
            with self.subTest(runtime=runtime, required=required):
                with self.assertRaises(RuntimeContractError) as caught:
                    negotiate_capabilities(runtime, required, dispatch="inline")
                self.assertEqual(message, str(caught.exception))

    def test_dispatch_is_inline_when_asked_or_when_the_runtime_cannot_delegate(self) -> None:
        cases = (
            ("claude-code", "generic", False, "subagents"),
            ("claude-code", "specialists", False, "subagents"),
            ("claude-code", "entrypoint", False, "subagents"),
            ("codex", "specialists", False, "subagents"),
            ("copilot-cli", "entrypoint", False, "copilot-host"),
            ("copilot-cli", "generic", False, "inline"),
            ("copilot-cli", "specialists", False, "inline"),
            ("claude-code", "generic", True, "inline"),
            ("codex", "entrypoint", True, "inline"),
            ("copilot-cli", "entrypoint", True, "inline"),
        )
        for runtime, kind, inline, dispatch in cases:
            with self.subTest(runtime=runtime, kind=kind, inline=inline):
                self.assertEqual(dispatch, choose_dispatch(runtime, kind, inline=inline))

    def test_runtime_auto_detection_uses_priority_and_the_shared_locator(self) -> None:
        with mock.patch(
            "review_runtime.find_executable",
            side_effect=lambda command: "C:/tools/codex.exe" if command == "codex" else None,
        ) as located:
            self.assertEqual("codex", resolve_runtime("auto"))
        self.assertEqual([mock.call("claude"), mock.call("codex")], located.call_args_list)
        with (
            mock.patch("review_runtime.find_executable", return_value=None),
            self.assertRaisesRegex(RuntimeContractError, "No supported"),
        ):
            resolve_runtime("auto")

    if sys.platform == "win32":

        def test_runtime_auto_detection_finds_a_cli_only_in_wingets_links_folder(self) -> None:
            # A sanitized environment: PATH is one empty folder and the local application data folder a fresh one, so
            # only the fixture's programs answer, as `deploy.py check` finds them.
            with tempfile.TemporaryDirectory(prefix="runtime locator ") as temporary:
                root = Path(temporary)
                on_path = root / "on path"
                links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
                on_path.mkdir()
                links.mkdir(parents=True)
                environment = {
                    "PATH": str(on_path),
                    "PATHEXT": ".COM;.EXE",
                    "LOCALAPPDATA": str(root / "local app data"),
                }
                with mock.patch.dict(os.environ, environment):
                    self.assertRaisesRegex(RuntimeContractError, "No supported", resolve_runtime, "auto")
                    for command, runtime in (("copilot", "copilot-cli"), ("codex", "codex"), ("claude", "claude-code")):
                        with self.subTest(command=command):
                            (links / f"{command}.exe").write_bytes(b"fixture")
                            self.assertEqual(runtime, resolve_runtime("auto"))
                    for program in links.iterdir():
                        program.unlink()
                    (on_path / "codex.exe").write_bytes(b"fixture")
                    self.assertEqual("codex", resolve_runtime("auto"), "a CLI on PATH is found there")

    else:

        def test_runtime_auto_detection_finds_a_cli_on_path_and_none_in_a_links_folder(self) -> None:
            # Off Windows no package manager's link folder is searched, so a CLI only in a WinGet-shaped folder that the
            # local application data variable names is not found, and one on PATH is.
            with tempfile.TemporaryDirectory(prefix="runtime locator ") as temporary:
                root = Path(temporary)
                on_path = root / "on path"
                links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
                on_path.mkdir()
                links.mkdir(parents=True)
                for command in ("copilot", "codex", "claude"):
                    (links / command).write_bytes(b"#!/bin/sh\n")
                    (links / command).chmod(0o755)
                environment = {"PATH": str(on_path), "LOCALAPPDATA": str(root / "local app data")}
                with mock.patch.dict(os.environ, environment):
                    self.assertRaisesRegex(RuntimeContractError, "No supported", resolve_runtime, "auto")
                    (on_path / "codex").write_bytes(b"#!/bin/sh\n")
                    (on_path / "codex").chmod(0o755)
                    self.assertEqual("codex", resolve_runtime("auto"), "a CLI on PATH is found there")

    def test_runtime_auto_prefers_the_stated_host_over_path(self) -> None:
        # A Codex session on a machine that also has claude installed must not resolve to claude-code.
        with mock.patch("review_runtime.find_executable", return_value="C:/tools/claude.exe") as which:
            self.assertEqual("codex", resolve_runtime("auto", "codex"))
            self.assertEqual("copilot-cli", resolve_runtime("auto", "copilot-cli"))
            which.assert_not_called()
            self.assertEqual("claude-code", resolve_runtime("auto", None), "no host keeps the PATH search")
        with self.assertRaisesRegex(RuntimeContractError, "Unknown runtime host"):
            resolve_runtime("auto", "cursor")

    def test_configured_runtime_overrides_the_stated_host(self) -> None:
        with mock.patch("review_runtime.find_executable", return_value=None):
            self.assertEqual("copilot-cli", resolve_runtime("copilot-cli", "claude-code"))
            self.assertEqual("codex", resolve_runtime("codex", None))

    def test_git_symlink_entries_are_rejected_before_materialization(self) -> None:
        manifest = self._manifest()

        def runner(arguments: Sequence[str], timeout: float) -> GitResult:
            if "ls-tree" in arguments:
                return GitResult(
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
        # in the caller on POSIX. The shared git runner takes bytes from the bounded layer and decodes them itself,
        # which gives both platforms the same, lossless path.
        finished = bounded_process.Finished(0, b"caf\xe9\r\nok\r\n", b"fatal: caf\xe9\n")
        with mock.patch("git_client.run_bounded", return_value=finished) as run:
            result = review_runtime.subprocess_runner(["git", "show", "HEAD:latin1.md"], 60)
        git = located_command(["git"])[0]
        self.assertEqual(([git, "show", "HEAD:latin1.md"], 60), run.call_args.args)
        self.assertEqual(b"caf\xe9\r\nok\r\n", result.stdout.encode("utf-8", "surrogateescape"))
        self.assertEqual("fatal: caf�\n", result.stderr)
        self.assertEqual(0, result.returncode)

    def test_a_git_failure_never_carries_an_undecodable_byte_into_its_message(self) -> None:
        def runner(arguments: Sequence[str], timeout: float) -> GitResult:
            return GitResult(128, "caf\udce9", "")

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
            arguments: dict[str, Any] = {
                "expected_repository": "example/one",
                "expected_commit": head,
                "contents": False,
            }
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

    def _plumbing_repository(self, root: Path, entries: dict[bytes, tuple[str, bytes]]) -> tuple[Path, str]:
        """A checkout whose head commit holds `entries`, each raw path -> (mode, content), written with git plumbing,
        so a path need not be one Windows can create and no attribute, filter, or line-ending setting touches a byte.
        A gitlink's content is the commit it names."""
        checkout = root / "checkout"
        checkout.mkdir()
        self._git(checkout, "init", "-b", "main")
        self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
        records = b""
        for path, (mode, content) in entries.items():
            if mode == "160000":
                object_id = content
            else:
                object_id = self._git_bytes(checkout, "hash-object", "-w", "--stdin", data=content).strip()
            records += mode.encode("ascii") + b" " + object_id + b"\t" + path + b"\0"
        self._git_bytes(checkout, "update-index", "-z", "--index-info", data=records)
        tree = self._git(checkout, "write-tree")
        identity = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid")
        head = self._git(checkout, *identity, "commit-tree", tree, "-m", "fixture")
        self._git(checkout, "update-ref", "refs/heads/main", head)
        return checkout, head

    @staticmethod
    def _git_bytes(path: Path, *arguments: str, data: bytes = b"") -> bytes:
        result = subprocess.run(["git", "-C", str(path), *arguments], input=data, capture_output=True, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode("utf-8", "replace"))
        return result.stdout

    @staticmethod
    def _archived(archive: tarfile.TarFile, name: str) -> bytes:
        member = archive.extractfile(name)
        if member is None:
            raise AssertionError(f"{name} is not a file in the archive")
        return member.read()

    def test_source_snapshot_holds_the_exact_blobs_whatever_attributes_and_configuration_say(self) -> None:
        files = {
            b".gitattributes": (
                b"hidden.py export-ignore\nstamp.py export-subst\nupper.txt filter=upper\n*.txt text eol=crlf\n"
            ),
            b"hidden.py": b"print('hidden')\n",
            b"stamp.py": b"VERSION = '$Format:%H$'\n",
            b"upper.txt": b"lower case\n",
            b"lines.txt": b"one\ntwo\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {path: ("100644", content) for path, content in files.items()}
            )
            self._git(checkout, "config", "core.autocrlf", "true")
            self._git(checkout, "config", "filter.upper.smudge", "tr a-z A-Z")
            # `git archive`, which the snapshot was once extracted from, drops or rewrites every file but one here.
            with tarfile.open(fileobj=io.BytesIO(self._git_bytes(checkout, "archive", "--format=tar", head))) as tar:
                self.assertNotIn("hidden.py", tar.getnames())
                self.assertEqual(f"VERSION = '{head}'\r\n".encode("ascii"), self._archived(tar, "stamp.py"))
                self.assertEqual(b"LOWER CASE\r\n", self._archived(tar, "upper.txt"))
                self.assertEqual(b"one\r\ntwo\r\n", self._archived(tar, "lines.txt"))
            snapshot = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, snapshot, changed_paths=["hidden.py"])
            for path, content in files.items():
                name = path.decode("ascii")
                self.assertEqual(content, (snapshot / name).read_bytes(), name)
                self.assertEqual(hashlib.sha256(content).hexdigest(), metadata["source_hashes"][name], name)
            self.assertEqual({}, metadata["excluded_paths"])
            verify_source_snapshot(snapshot, expected_repository="example/one", expected_commit=head)
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/hidden.py b/hidden.py\nnew file mode 100644\nindex 0000000..1111111\n"
                "--- /dev/null\n+++ b/hidden.py\n@@ -0,0 +1 @@\n+print('hidden')\n",
                encoding="utf-8",
            )
            self.assertEqual([], review_runtime.unavailable_sources(diff, metadata), "reviewers have its source")

    def test_an_undecodable_name_is_an_unsafe_path_and_a_coverage_gap_only_where_changed(self) -> None:
        # Two bytes that open a UTF-8 sequence and never finish it: one U+FFFD each, as diff.patch shows them.
        name = "src/bad\ufffd\ufffd.py"
        self.assertEqual(
            name, github_client.replace_undecodable(b"src/bad\xe2\x82.py".decode("utf-8", "surrogateescape"))[0]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"src/ok.py": ("100644", b"ok\n"), b"src/bad\xe2\x82.py": ("100644", b"bad\n")}
            )
            snapshot = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, snapshot, changed_paths=[name])
            self.assertEqual(["src/ok.py"], list(metadata["source_hashes"]))
            self.assertEqual({name: "unsafe-path"}, metadata["excluded_paths"])
            self.assertEqual(["ok.py"], [path.name for path in (snapshot / "src").iterdir()])
            verify_source_snapshot(snapshot, expected_repository="example/one", expected_commit=head)
            # validate-reviewer measures with the same rules, so it reports the exclusion rather than failing.
            measured = review_runtime.measure_source_snapshot(
                checkout, head, destination=snapshot, changed_paths=[name]
            )
            self.assertEqual((1, 3, {"unsafe-path": 1}), (measured.files, measured.bytes, measured.excluded))
            diff = root / "diff.patch"
            for changed, unavailable, verdict in ((name, [name], "INCOMPLETE"), ("src/ok.py", [], "APPROVED")):
                diff.write_text(
                    f"diff --git a/{changed} b/{changed}\nindex 1111111..2222222 100644\n"
                    f"--- a/{changed}\n+++ b/{changed}\n@@ -1 +1 @@\n-old\n+new\n",
                    encoding="utf-8",
                )
                self.assertEqual(unavailable, review_runtime.unavailable_sources(diff, metadata), changed)
                self.assertEqual(verdict, calculate_verdict([], {}, unavailable), changed)
            # git quotes the name with octal escapes, which the diff parser decodes with one U+FFFD for the whole
            # truncated sequence, not one per byte; the two forms still match.
            octal = "".join(f"{chr(92)}{byte:o}" for byte in bytes([0xE2, 0x82]))
            quoted = f"src/bad{octal}.py"
            diff.write_text(
                f'diff --git "a/{quoted}" "b/{quoted}"\nindex 1111111..2222222 100644\n'
                f'--- "a/{quoted}"\n+++ "b/{quoted}"\n@@ -1 +1 @@\n-old\n+new\n',
                encoding="utf-8",
            )
            unavailable = review_runtime.unavailable_sources(diff, metadata)
            self.assertEqual([f"src/bad{chr(0xFFFD)}.py"], unavailable)
            self.assertEqual("INCOMPLETE", calculate_verdict([], {}, unavailable))

    def test_source_snapshot_excludes_links_and_writes_nothing_for_a_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root,
                {
                    b"src/A.cs": ("100644", b"class A {}\n"),
                    b"tools/run.sh": ("100755", b"echo run\n"),
                    b"tools/cache": ("120000", b"/home/dev/.cache/tool"),
                    b"vendor/lib": ("160000", b"d" * 40),
                },
            )
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertEqual({"tools/cache": "symbolic-link"}, metadata["excluded_paths"])
            self.assertEqual(["src/A.cs", "tools/run.sh"], sorted(metadata["source_hashes"]))
            self.assertEqual(
                sorted([SOURCE_SNAPSHOT_MANIFEST, "src/A.cs", "tools/run.sh"]),
                sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()),
            )
            self.assertFalse((destination / "tools" / "cache").exists(), "nothing is written for a link")
            self.assertFalse((destination / "vendor").exists(), "a submodule has no blob, so nothing is written")
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            # Deliberate, like binary and agent-instruction: reviewed from the diff, never a coverage gap.
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/tools/cache b/tools/cache\nnew file mode 120000\nindex 0000000..1111111\n"
                "--- /dev/null\n+++ b/tools/cache\n@@ -0,0 +1 @@\n+/home/dev/.cache/tool\n"
                "\\ No newline at end of file\n",
                encoding="utf-8",
            )
            self.assertEqual([], review_runtime.unavailable_sources(diff, metadata))
        self.assertEqual(
            {
                "agent-instruction",
                "binary",
                "configured",
                "file-size-limit",
                "unsafe-path",
                "symbolic-link",
                "non-regular",
                "unextractable",
            },
            review_runtime.SNAPSHOT_EXCLUSION_REASONS,
        )
        self.assertEqual(
            {"configured", "file-size-limit", "unsafe-path", "unextractable"}, review_runtime.COVERAGE_GAP_REASONS
        )

    def test_source_snapshot_still_refuses_a_link_at_the_reserved_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {SOURCE_SNAPSHOT_MANIFEST.encode("ascii"): ("120000", b"/etc/passwd")}
            )
            destination = root / "snapshot"
            with self.assertRaisesRegex(RuntimeContractError, "reserved snapshot path"):
                materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertFalse(destination.exists())

    def test_a_blob_reader_stopped_early_ends_git_and_releases_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            # Far more than a pipe buffers, so git is still writing when the reader stops.
            checkout, head = self._plumbing_repository(root, {b"big.txt": ("100644", b"x" * (1024 * 1024))})
            blob = self._git(checkout, "rev-parse", f"{head}:big.txt")
            reader = review_runtime.git_blob_reader(checkout, [blob] * 32)
            self.assertEqual(1024 * 1024, len(next(reader)))
            reader.close()
            # Git for Windows' git.exe is a launcher: ending only it leaves the real git running in the checkout, and
            # Windows refuses to move a folder a running process works in.
            checkout.rename(root / "moved")
            self.assertFalse(checkout.exists())

    def test_source_snapshot_fails_closed_when_the_blob_reader_answers_short_or_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"a.txt": ("100644", b"a\n"), b"b.txt": ("100644", b"b\n")}
            )
            blob = self._git(checkout, "rev-parse", f"{head}:a.txt")
            self.assertEqual([b"a\n", b"a\n"], list(review_runtime.git_blob_reader(checkout, [blob, blob])))
            with self.assertRaisesRegex(RuntimeContractError, "missing"):
                list(review_runtime.git_blob_reader(checkout, ["0" * 40]))

            def short(_checkout: Path, _blobs: Sequence[str]) -> Iterator[bytes]:
                yield b"a\n"

            def wrong(_checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
                for _blob in blobs:
                    yield b"rewritten\n"

            for reader, message in ((short, "fewer blobs"), (wrong, "size")):
                destination = root / "snapshot"
                with self.subTest(message=message), self.assertRaisesRegex(RuntimeContractError, message):
                    materialize_source_snapshot(checkout, "example/one", head, destination, blob_reader=reader)
                self.assertFalse(destination.exists())

    def test_source_snapshot_materialization_enforces_file_count_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"one.txt": ("100644", b"x"), b"two.txt": ("100644", b"x")}
            )
            destination = root / "snapshot"
            with (
                mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_FILES", 1),
                self.assertRaisesRegex(RuntimeContractError, "file-count"),
            ):
                materialize_source_snapshot(checkout, "example/one", head, destination)
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
            checkout, _, head = self._repository(Path(temporary))
            with self.assertRaisesRegex(RuntimeContractError, "Pull-request refs"):
                resolve_reviewer_commit(checkout, "refs/pull/1/head", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "pull-request head"):
                resolve_reviewer_commit(checkout, "HEAD", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "origin mismatch"):
                verify_checkout_remote(checkout, "different/one")


class AnsweringGit:
    """A git runner that records each command and answers it from `answers`, one result or error per call in order,
    else with success and the command's arguments as its output, so each answer says which call gave it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers: dict[tuple[str, ...], list[GitResult | GitError]] = {}

    def __call__(self, command: Sequence[str], timeout: float) -> GitResult:
        key = tuple(command)
        self.calls.append(key)
        answers = self.answers.get(key[3:])
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, GitError):
                raise answer
            return answer
        return GitResult(0, f"{' '.join(key[3:])} #{len(self.calls)}\n", "")


FULL_COMMIT = "1" * 40
FULL_SHA256_COMMIT = "2" * 64


def in_checkout(*arguments: str, checkout: str = "C:/checkout") -> tuple[str, ...]:
    return ("git", "-C", checkout, *arguments)


class CheckoutRemoteTests(GitTestCase):
    """The checkout's origin is the configured repository only when the whole URL is a github.com remote's."""

    def verify(self, url: str, repository: str = "example/one") -> None:
        git = AnsweringGit()
        git.answers[("remote", "get-url", "origin")] = [GitResult(0, f"{url}\n", "")]
        verify_checkout_remote(Path("C:/checkout"), repository, git)

    def test_each_github_com_form_of_the_repository_is_accepted(self) -> None:
        for url in (
            "https://github.com/example/one.git",
            "https://github.com/Example/One",
            "git@github.com:example/one.git",
            "git@github.com:example/one",
            "ssh://git@github.com/example/one.git",
        ):
            with self.subTest(url=url):
                self.verify(url)

    def test_a_remote_on_another_host_is_refused_whatever_its_path_names(self) -> None:
        for url in (
            "https://example.invalid/github.com/example/one",
            "https://example.invalid/mirror/github.com:example/one.git",
            "git@example.invalid:github.com/example/one",
            "https://github.com.example.invalid/example/one",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeContractError, "not a recognizable GitHub"):
                self.verify(url)
        with self.assertRaisesRegex(RuntimeContractError, "origin mismatch: expected example/two, found example/one"):
            self.verify("https://github.com/example/one.git", "example/two")


class PrepareGitRunnerTests(GitTestCase):
    """One prepare's git runner asks an identical query of origin or of a full object id once, until a command that
    is not a read; a ref, an abbreviation, a failed answer, and another prepare's runner are asked again."""

    ORIGIN = ("remote", "get-url", "origin")
    COMMIT = ("rev-parse", "--verify", f"{FULL_COMMIT}^{{commit}}")
    REUSED = (
        ORIGIN,
        COMMIT,
        ("rev-parse", "--verify", f"{FULL_SHA256_COMMIT}^{{commit}}"),
        ("rev-parse", "--verify", FULL_COMMIT),
        ("cat-file", "-e", f"{FULL_COMMIT}^{{commit}}"),
        ("ls-tree", "-z", FULL_COMMIT, "--", "review/rules.md", "review/python.md"),
        ("ls-tree", "-r", "-z", "-l", "--full-tree", FULL_COMMIT),
        ("ls-tree", "-r", "--name-only", "-z", FULL_SHA256_COMMIT),
    )
    # Reads whose answer a ref, an abbreviation, or the network can change, or that no prepare repeats.
    ASKED_EACH_TIME = (
        ("rev-parse", "--verify", "refs/heads/reviewers^{commit}"),
        ("rev-parse", "--verify", "abc1234^{blob}"),
        ("rev-parse", "--verify", f"{FULL_COMMIT[:39]}^{{commit}}"),
        ("rev-parse", "--verify", f"{FULL_COMMIT}~1"),
        ("cat-file", "-e", "main^{commit}"),
        ("cat-file", "-s", FULL_COMMIT),
        ("cat-file", "blob", FULL_COMMIT),
        ("ls-tree", "-z", "HEAD", "--", FULL_COMMIT),
        ("ls-remote", "--symref", "origin", "HEAD"),
        ("remote", "get-url", "upstream"),
    )
    # Any command not known to be a read is taken as one that may write, even one that only reads.
    NOT_READS = (
        in_checkout("fetch", "--no-tags", "--quiet", "origin", "refs/pull/12/head"),
        in_checkout("remote", "set-url", "origin", "https://github.com/other/repo.git"),
        in_checkout("remote", "get-url", "--push", "origin"),
        in_checkout("config", "remote.origin.url", "https://github.com/other/repo.git"),
        in_checkout("update-ref", "refs/heads/reviewers", FULL_COMMIT),
        ("git", "rev-parse", "--verify", f"{FULL_COMMIT}^{{commit}}"),  # in no named checkout
    )

    def test_an_identical_query_is_asked_once(self) -> None:
        for arguments in self.REUSED:
            with self.subTest(arguments=arguments):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                first = runner(in_checkout(*arguments), 30)
                self.assertIs(first, runner(in_checkout(*arguments), 60), "the first answer, whatever the time limit")
                self.assertEqual([in_checkout(*arguments)], git.calls)

    def test_another_checkout_or_another_object_is_asked_again(self) -> None:
        git = AnsweringGit()
        runner = PrepareGitRunner(git)
        commands = [
            in_checkout(*self.ORIGIN),
            in_checkout(*self.ORIGIN, checkout="C:/other"),
            in_checkout(*self.COMMIT),
            in_checkout("rev-parse", "--verify", f"{FULL_COMMIT}^{{tree}}"),
            in_checkout("rev-parse", "--verify", f"{'3' * 40}^{{commit}}"),
            in_checkout("ls-tree", "-z", FULL_COMMIT, "--", "review/rules.md"),
            in_checkout("ls-tree", "-z", FULL_COMMIT, "--", "review/python.md"),
        ]
        for command in commands * 2:
            runner(command, 30)
        self.assertEqual(commands, git.calls)

    def test_a_ref_an_abbreviation_and_any_other_read_are_asked_every_time(self) -> None:
        for arguments in self.ASKED_EACH_TIME:
            with self.subTest(arguments=arguments):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                runner(in_checkout(*arguments), 30)
                runner(in_checkout(*arguments), 30)
                self.assertEqual([in_checkout(*arguments)] * 2, git.calls)

    def test_a_read_leaves_the_kept_answers(self) -> None:
        git = AnsweringGit()
        runner = PrepareGitRunner(git)
        reads = [in_checkout(*arguments) for arguments in self.ASKED_EACH_TIME]
        for command in (in_checkout(*self.ORIGIN), *reads, in_checkout(*self.ORIGIN)):
            runner(command, 30)
        self.assertEqual([in_checkout(*self.ORIGIN), *reads], git.calls)

    def test_a_failed_answer_is_asked_again(self) -> None:
        git = AnsweringGit()
        present = ("cat-file", "-e", f"{FULL_COMMIT}^{{commit}}")
        git.answers[present] = [GitResult(1, "", "fatal: missing")]
        runner = PrepareGitRunner(git)
        self.assertEqual(
            [1, 0, 0], [runner(in_checkout(*present), 30).returncode for _ in range(3)], "asked twice, then kept"
        )
        self.assertEqual([in_checkout(*present)] * 2, git.calls)

    def test_a_command_that_is_not_a_read_drops_every_kept_answer(self) -> None:
        for write in self.NOT_READS:
            with self.subTest(write=write):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                queries = [in_checkout(*self.ORIGIN), in_checkout(*self.COMMIT)]
                for command in (*queries, *queries, write, *queries, *queries):
                    runner(command, 30)
                self.assertEqual([*queries, write, *queries], git.calls)

    def test_a_write_that_fails_or_cannot_run_drops_them_too(self) -> None:
        fetch = ("fetch", "--no-tags", "--quiet", "origin", "refs/heads/main")
        for answer in (GitResult(1, "", "fatal: cannot fetch"), GitError("git timed out", kind="timeout")):
            with self.subTest(answer=answer):
                git = AnsweringGit()
                git.answers[fetch] = [answer]
                runner = PrepareGitRunner(git)
                runner(in_checkout(*self.ORIGIN), 30)
                with contextlib.suppress(GitError):
                    runner(in_checkout(*fetch), 30)
                runner(in_checkout(*self.ORIGIN), 30)
                self.assertEqual([in_checkout(*self.ORIGIN), in_checkout(*fetch), in_checkout(*self.ORIGIN)], git.calls)

    def test_another_runner_keeps_nothing_of_the_first(self) -> None:
        git = AnsweringGit()
        PrepareGitRunner(git)(in_checkout(*self.ORIGIN), 30)
        PrepareGitRunner(git)(in_checkout(*self.ORIGIN), 30)
        self.assertEqual([in_checkout(*self.ORIGIN)] * 2, git.calls)

    def test_a_kept_answer_is_still_judged_on_every_check(self) -> None:
        git = AnsweringGit()
        git.answers[self.ORIGIN] = [GitResult(0, "https://github.com/example/one.git\n", "")]
        git.answers[self.COMMIT] = [GitResult(0, f"{FULL_COMMIT}\n", "")]
        runner = PrepareGitRunner(git)
        checkout = Path("C:/checkout")
        verify_checkout_remote(checkout, "example/one", runner)
        with self.assertRaisesRegex(RuntimeContractError, "origin mismatch: expected other/repo, found example/one"):
            verify_checkout_remote(checkout, "other/repo", runner)
        self.assertEqual(FULL_COMMIT, resolve_reviewer_commit(checkout, FULL_COMMIT, head_sha="4" * 40, runner=runner))
        with self.assertRaisesRegex(RuntimeContractError, "resolves to the pull-request head"):
            resolve_reviewer_commit(checkout, FULL_COMMIT, head_sha=FULL_COMMIT, runner=runner)
        self.assertEqual(
            [in_checkout(*self.ORIGIN, checkout=str(checkout)), in_checkout(*self.COMMIT, checkout=str(checkout))],
            git.calls,
        )


def git_blob_id(content: bytes) -> str:
    """The SHA-1 git blob id of `content`, computed here rather than by the code under test."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def entrypoint_files(*resources: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "fixture-review",
        "protocol_version": 1,
        "supports": ["initial"],
        "required_capabilities": ["read-diff", "write-result"],
        "entrypoint": "review/SKILL.md",
        "resources": list(resources),
        "agent_profiles": [],
    }


class CommittedReadTests(GitTestCase):
    """Reviewer files come from a commit's tree in batches, one `git ls-tree` per commit for their entries and one
    `git cat-file --batch` for their bytes by the blob ids those entries gave, with every refusal before any read; and a
    diff's base blob is sized before its content is asked for."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q", "-b", "main")
        self.calls: list[list[str]] = []
        self.batches: list[list[str]] = []

    def git(self, *arguments: str, data: bytes | None = None, index: Path | None = None) -> str:
        environment = None if index is None else {**os.environ, "GIT_INDEX_FILE": str(index)}
        return (
            subprocess.run(
                ["git", "-C", str(self.checkout), *arguments],
                input=data,
                capture_output=True,
                check=True,
                env=environment,
            )
            .stdout.decode("ascii")
            .strip()
        )

    def commit(self, entries: Mapping[str, bytes | tuple[str, str]], *parents: str) -> str:
        """A commit whose tree holds exactly `entries`: a path's bytes as a regular file, or (mode, object id)."""
        index = self.root / f"index-{len(list(self.root.glob('index-*')))}"
        for path, entry in entries.items():
            mode, object_id = entry if isinstance(entry, tuple) else ("100644", self.blob(entry))
            self.git("update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}", index=index)
        tree = self.git("write-tree", index=index)
        return self.git("commit-tree", tree, *(part for parent in parents for part in ("-p", parent)), "-m", "fixture")

    def blob(self, content: bytes) -> str:
        return self.git("hash-object", "-w", "--stdin", data=content)

    def runner(self, arguments: Sequence[str], timeout: float) -> GitResult:
        self.calls.append(list(arguments[3:]))
        return review_runtime.subprocess_runner(arguments, timeout)

    def reader(self, checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
        self.batches.append(list(blobs))
        yield from review_runtime.git_blob_reader(checkout, blobs)

    def materialize(self, commit: str, manifest: dict[str, Any], name: str = "reviewer", **options: Any) -> Path:
        destination = self.root / name
        self.hashes = materialize_reviewer(
            self.checkout, commit, manifest, destination, runner=self.runner, blob_reader=self.reader, **options
        )
        return destination

    def test_each_commit_s_entries_come_from_one_ls_tree_and_every_blob_from_one_batch(self) -> None:
        crlf, latin1, same = b"Shared\r\nrules\r\n", b"caf\xe9\n", b"same text\n"
        trusted_g1, base_g1, g2 = b"trusted guideline\n", b"base guideline\n", b"guideline two\n"
        trusted = self.commit(
            {
                "docs/shared.md": crlf,
                "docs/same.md": same,
                "agents/p.md": latin1,
                "docs/g1.md": trusted_g1,
                "docs/g2.md": g2,
                "docs/copy.md": same,
            }
        )
        base = self.commit({"docs/g1.md": base_g1, "README.md": b"base\n"})
        manifest = {
            "schema_version": 2,
            "id": "fixture",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation"],
            "resources": ["docs/shared.md", "docs/same.md"],
            "specialists": [
                {
                    "id": "p-review",
                    "category": "P",
                    "profile": "agents/p.md",
                    "include": [r"\.p$"],
                    "exclude": [],
                    "resources": ["docs/g1.md", "docs/g2.md", "docs/copy.md"],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        destination = self.materialize(trusted, manifest, guideline_commit=base)

        at_trusted = ["docs/shared.md", "docs/same.md", "agents/p.md", "docs/g2.md", "docs/copy.md"]
        self.assertEqual(
            [
                ["rev-parse", "--verify", f"{base}^{{commit}}"],
                ["ls-tree", "-z", base, "--", "docs/g1.md", "docs/g2.md", "docs/copy.md"],
                ["ls-tree", "-z", trusted, "--", *at_trusted],
            ],
            self.calls,
        )
        # One batch, each blob once: docs/copy.md holds the bytes docs/same.md does.
        self.assertEqual([[git_blob_id(blob) for blob in (crlf, same, latin1, base_g1, g2)]], self.batches)
        expected = {
            "docs/shared.md": crlf,
            "docs/same.md": same,
            "agents/p.md": latin1,
            "docs/g1.md": base_g1,
            "docs/g2.md": g2,
            "docs/copy.md": same,
        }
        self.assertEqual(expected, {path: (destination / path).read_bytes() for path in expected})
        self.assertEqual({path: hashlib.sha256(content).hexdigest() for path, content in expected.items()}, self.hashes)
        self.assertEqual(
            {"docs/g1.md": base, "docs/g2.md": trusted, "docs/copy.md": trusted},
            json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["guideline_sources"],
        )

    def test_a_refused_entry_anywhere_in_a_batch_fails_before_any_blob_is_read(self) -> None:
        submodule = self.commit({"lib.txt": b"vendored\n"})
        trusted = self.commit(
            {
                "review/SKILL.md": b"# Review\n",
                "docs/a.md": b"first\n",
                "docs/z.md": b"last\n",
                "docs/link.md": ("120000", self.blob(b"a.md")),
                "vendor/lib": ("160000", submodule),
            }
        )
        cases = (
            ("docs/link.md", "Declared reviewer file is not a regular file: docs/link.md", 1),
            ("vendor/lib", "Declared reviewer file is not a regular file: vendor/lib", 1),
            ("docs/gone.md", "Declared reviewer file is missing: docs/gone.md", 1),
            # A folder beside a path under it takes a call of its own, or ls-tree would list the folder's entries.
            ("docs", "Declared reviewer file is not a regular file: docs", 2),
        )
        for index, (refused, message, listings) in enumerate(cases):
            with self.subTest(refused):
                self.calls.clear()
                self.batches.clear()
                destination = self.root / f"refused-{index}"
                with self.assertRaises(RuntimeContractError) as raised:
                    self.materialize(trusted, entrypoint_files("docs/a.md", refused, "docs/z.md"), destination.name)
                self.assertEqual(message, str(raised.exception))
                self.assertEqual(listings, len(self.calls))
                self.assertEqual([], self.batches)
                self.assertFalse(destination.exists())

    def test_a_guideline_the_base_holds_as_no_regular_file_is_refused_there(self) -> None:
        trusted = self.commit({"docs/g.md": b"trusted\n"})
        manifest = {
            "schema_version": 2,
            "id": "fixture",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation"],
            "resources": [],
            "specialists": [
                {
                    "id": "g-review",
                    "category": "G",
                    "profile": "suite:design-review",
                    "include": [r"\.g$"],
                    "exclude": [],
                    "resources": ["docs/g.md"],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        base = self.commit({"docs/g.md": ("120000", self.blob(b"../secret"))})
        with self.assertRaisesRegex(RuntimeContractError, "^Declared reviewer file is not a regular file: docs/g.md$"):
            self.materialize(trusted, manifest, guideline_commit=base)
        self.assertEqual([], self.batches)
        self.assertEqual([base], [call[2] for call in self.calls if call[0] == "ls-tree"], "never the trusted copy")

    def test_bytes_that_do_not_hash_to_the_listed_blob_are_refused(self) -> None:
        trusted = self.commit({"review/SKILL.md": b"# Review\n", "docs/a.md": b"rules\n"})

        def swapped(checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
            yield b"# Review\n"
            yield b"other rules\n"

        def missing(checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
            yield b"# Review\n"
            raise RuntimeContractError(f"Blob {blobs[1]} is missing from the checkout's object store")

        for name, reader in (("swapped", swapped), ("missing", missing)):
            with self.subTest(name), self.assertRaises(RuntimeContractError) as raised:
                materialize_reviewer(
                    self.checkout, trusted, entrypoint_files("docs/a.md"), self.root / name, blob_reader=reader
                )
            self.assertEqual("Cannot read declared reviewer file: docs/a.md", str(raised.exception))
            self.assertFalse((self.root / name).exists())

    def test_a_long_list_of_paths_is_split_across_listings_and_read_in_one_batch(self) -> None:
        names = [f"docs/{index:02d}-{'x' * 30}.md" for index in range(6)]
        trusted = self.commit({"review/SKILL.md": b"# Review\n", **{name: name.encode() for name in names}})
        with mock.patch.object(review_runtime, "LS_TREE_PATH_UNITS", 100):
            destination = self.materialize(trusted, entrypoint_files(*names))
        listed = [call[4:] for call in self.calls]
        self.assertEqual(["review/SKILL.md", *names], [path for call in listed for path in call])
        self.assertTrue(all(sum(len(path) + 3 for path in call) <= 100 for call in listed), listed)
        self.assertEqual(4, len(listed))
        self.assertEqual(1, len(self.batches))
        self.assertEqual([name.encode() for name in names], [(destination / name).read_bytes() for name in names])

    def test_a_single_reviewer_file_is_read_by_its_listed_blob_through_the_runner(self) -> None:
        content = b"{\r\n}\r\n"
        trusted = self.commit({".review/manifest.json": content})
        self.assertEqual(
            content, review_runtime._read_git_file(self.checkout, trusted, ".review/manifest.json", self.runner)
        )
        self.assertEqual(
            [
                ["ls-tree", "-z", trusted, "--", ".review/manifest.json"],
                ["cat-file", "blob", git_blob_id(content)],
            ],
            self.calls,
        )

    def test_the_changed_file_limit_is_sixteen_mebibytes(self) -> None:
        self.assertEqual(16_777_216, review_runtime.MAX_CHANGED_FILE_BYTES)

    def test_a_base_blob_over_the_limit_is_refused_by_its_size_without_its_content_being_asked_for(self) -> None:
        at_limit, over = b"a" * 16_777_216, b"a" * 16_777_217
        for content in (at_limit, over):
            self.blob(content)
        limit = review_runtime.MAX_CHANGED_FILE_BYTES
        for content, expected in ((at_limit, at_limit), (over, None)):
            blob = git_blob_id(content)
            with self.subTest(size=len(content)):
                self.calls.clear()
                self.assertEqual(
                    expected, review_runtime.read_blob(self.checkout, blob[:7], self.runner, maximum=limit)
                )
                read = [["cat-file", "blob", blob]] if expected is not None else []
                self.assertEqual(
                    [["rev-parse", "--verify", f"{blob[:7]}^{{blob}}"], ["cat-file", "-s", blob], *read], self.calls
                )
        self.calls.clear()
        self.assertEqual(over, review_runtime.read_blob(self.checkout, git_blob_id(over)[:7], self.runner))
        self.assertNotIn("-s", [call[1] for call in self.calls], "no limit asks for no size")

    def test_a_base_blob_id_that_names_no_single_blob_is_refused(self) -> None:
        commit = self.commit({"docs/a.md": b"rules\n"})
        tree = self.git("rev-parse", f"{commit}^{{tree}}")
        for abbreviation in ("not-an-id", "abc", tree[:12], "0123456789abcdef"):
            with self.subTest(abbreviation), self.assertRaises(RuntimeContractError):
                review_runtime.read_blob(self.checkout, abbreviation, self.runner, maximum=1024)

        def unsized(arguments: Sequence[str], timeout: float) -> GitResult:
            if list(arguments[3:5]) == ["cat-file", "-s"]:
                return GitResult(0, "12 bytes\n", "")
            return review_runtime.subprocess_runner(arguments, timeout)

        blob = git_blob_id(b"rules\n")
        with self.assertRaisesRegex(RuntimeContractError, f"^The checkout did not return the size of blob {blob}$"):
            review_runtime.read_blob(self.checkout, blob[:7], unsized, maximum=1024)


class ReviewOperationTests(GitTestCase):
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

    def test_pull_metadata_and_selectors_of_the_wrong_kind_are_field_errors(self) -> None:
        def changed(**fields: Any) -> dict:
            return {**self._pull(42), **fields}

        merged = self._pull(42, "MERGED", "2026-01-05T00:00:00Z")
        cases: tuple[tuple[Any, str, str, str], ...] = (
            ([], "pull", "an object", "an array"),
            (changed(extra=1), "pull", "an object of known fields", 'unknown field "extra"'),
            (
                {key: value for key, value in self._pull(42).items() if key != "title"},
                "pull",
                "an object with field title",
                "an object without it",
            ),
            (changed(number="42"), "pull.number", "an integer of at least 1", 'string "42"'),
            (changed(number=True), "pull.number", "an integer of at least 1", "boolean true"),
            (changed(number=42.0), "pull.number", "an integer of at least 1", "number 42.0"),
            (changed(number=0), "pull.number", "an integer of at least 1", "integer 0"),
            (changed(state="open"), "pull.state", 'one of "OPEN", "MERGED"', 'string "open"'),
            (changed(state=["OPEN"]), "pull.state", 'one of "OPEN", "MERGED"', "an array"),
            (changed(isDraft="false"), "pull.isDraft", "a boolean", 'string "false"'),
            (changed(isDraft=0), "pull.isDraft", "a boolean", "integer 0"),
            (changed(title=7), "pull.title", "a non-empty string", "integer 7"),
            (changed(url=""), "pull.url", "a non-empty string", "an empty string"),
            (changed(headRefOid={}), "pull.headRefOid", "a non-empty string", "an object"),
            (changed(baseRefName=None), "pull.baseRefName", "a non-empty string", "null"),
            (changed(body=["text"]), "pull.body", "a string", "an array"),
            ({**merged, "mergedAt": None}, "pull.mergedAt", "a non-empty string", "null"),
            ({**merged, "mergedAt": 20260105}, "pull.mergedAt", "a non-empty string", "integer 20260105"),
        )
        for value, path, expected, found in cases:
            with self.subTest(path=path, found=found):
                with self.assertRaises(ReviewOperationFieldError) as context:
                    validate_pull(value)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
        selectors: tuple[tuple[Any, str], ...] = (
            (42, "integer 42"),
            (None, "null"),
            ("", "an empty string"),
            (["a/b#1"], "an array"),
        )
        for selector, found in selectors:
            with self.subTest(selector=selector), self.assertRaises(ReviewOperationFieldError) as context:
                parse_pull_selector(selector)
            self.assertEqual(field_message("pull selector", "a non-empty string", found), str(context.exception))

    def test_selection_skips_current_heads_and_old_merges(self) -> None:
        pulls = [
            self._pull(1),
            self._pull(2, "MERGED", "2025-12-01T00:00:00Z"),
            self._pull(3, "MERGED", "2026-01-03T00:00:00Z"),
        ]
        selected = select_eligible_pulls(
            pulls,
            merged_since=date(2026, 1, 1),
            reviewed_heads=lambda numbers: {1: "c" * 40},
        )
        self.assertEqual([3], [item["number"] for item in selected])

    def test_selection_reads_the_archive_only_for_pulls_past_the_state_and_watermark_filters(self) -> None:
        draft = self._pull(4)
        draft["isDraft"] = True
        pulls = [
            self._pull(5, "MERGED", "2026-01-01T00:00:00Z"),  # merged on the watermark day
            self._pull(1),
            draft,
            # Merged before the watermark: a later comment or label lists it again, but it stays out.
            self._pull(2, "MERGED", "2025-12-31T23:59:59Z"),
            self._pull(3, "MERGED", "2026-01-03T00:00:00Z"),
        ]
        looked_up: list[list[int]] = []

        def heads(numbers: list[int]) -> dict[int, str]:
            looked_up.append(list(numbers))
            return {5: pulls[0]["headRefOid"]}

        selected = select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads)
        self.assertEqual([[1, 3, 5]], looked_up)
        self.assertEqual([1, 3], [item["number"] for item in selected])

        looked_up.clear()
        forced = select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads, force=True)
        self.assertEqual([], looked_up)
        self.assertEqual([1, 3, 5], [item["number"] for item in forced])

    def test_selection_refuses_duplicate_and_malformed_pulls_before_reading_the_archive(self) -> None:
        def heads(numbers: list[int]) -> dict[int, str]:
            raise AssertionError("the archive is read only after every pull is validated")

        for pulls in ([self._pull(1), self._pull(1)], [self._pull(1), {"number": 2}]):
            with self.subTest(pulls=pulls), self.assertRaises(ReviewOperationError):
                select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads)

    def test_watermark_retains_unreviewed_merges(self) -> None:
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
            ),
        )

    def test_watermark_uses_the_earliest_pending_merge_date(self) -> None:
        self.assertEqual(
            date(2026, 1, 4),
            safe_watermark(
                previous=date(2026, 1, 1),
                today=date(2026, 1, 10),
                eligible_merged=[
                    self._pull(3, "MERGED", "2026-01-08T00:00:00Z"),
                    self._pull(2, "MERGED", "2026-01-05T23:59:59Z"),
                ],
                completed_numbers=set(),
            ),
        )

    def test_watermark_advances_to_today_without_pending_merges(self) -> None:
        for merged in ([], [self._pull(2, "MERGED", "2026-01-05T00:00:00Z")]):
            with self.subTest(merged=merged):
                self.assertEqual(
                    date(2026, 1, 10),
                    safe_watermark(
                        previous=date(2026, 1, 1),
                        today=date(2026, 1, 10),
                        eligible_merged=merged,
                        completed_numbers={2},
                    ),
                )

    def test_watermark_never_moves_backwards(self) -> None:
        for previous, merged in (
            (date(2026, 1, 11), []),
            (date(2026, 1, 1), [self._pull(2, "MERGED", "2026-01-01T00:00:00Z")]),
        ):
            with self.subTest(previous=previous, merged=merged):
                self.assertEqual(
                    previous,
                    safe_watermark(
                        previous=previous,
                        today=date(2026, 1, 10),
                        eligible_merged=merged,
                        completed_numbers=set(),
                    ),
                )

    def test_watermark_refuses_malformed_pending_merge_dates(self) -> None:
        for merged_at in ("not-a-date", "2026-02-30T00:00:00Z", "", None):
            with (
                self.subTest(merged_at=merged_at),
                self.assertRaises(ReviewOperationError),
            ):
                safe_watermark(
                    previous=date(2026, 1, 1),
                    today=date(2026, 1, 10),
                    eligible_merged=[self._pull(2, "MERGED", merged_at)],
                    completed_numbers=set(),
                )

    def test_watermark_validates_completed_merge_metadata(self) -> None:
        pull = self._pull(2, "MERGED", "2026-01-05T00:00:00Z")
        del pull["headRefOid"]
        with self.assertRaisesRegex(
            ReviewOperationFieldError, field_pattern("pull", "an object with field headRefOid", "an object without it")
        ):
            safe_watermark(
                previous=date(2026, 1, 1),
                today=date(2026, 1, 10),
                eligible_merged=[pull],
                completed_numbers={2},
            )

    def test_today_is_the_utc_date_on_either_side_of_utc_midnight(self) -> None:
        # Seconds since the epoch, written out so the expected dates do not come from the code under test. East of
        # UTC the first is already the next local day; west of UTC the second is still the previous one.
        for seconds, expected in ((1773187199, date(2026, 3, 10)), (1773187200, date(2026, 3, 11))):
            with self.subTest(seconds=seconds), frozen_clock(seconds):
                self.assertEqual(expected, review_state.utc_today())

    def test_merge_dates_are_utc_dates_whatever_offset_the_time_carries(self) -> None:
        for merged_at, expected in (
            ("2026-03-10T23:59:59Z", date(2026, 3, 10)),
            ("2026-03-11T00:00:00Z", date(2026, 3, 11)),
            ("2026-03-11T01:30:00+02:00", date(2026, 3, 10)),  # the next local day east of UTC
            ("2026-03-10T20:30:00-05:00", date(2026, 3, 11)),  # the previous local day west of UTC
            ("2026-03-10T23:30:00", date(2026, 3, 10)),  # no offset is read as UTC
        ):
            with self.subTest(merged_at=merged_at):
                self.assertEqual(expected, merged_date(self._pull(2, "MERGED", merged_at)))

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
                base=archive_base(None, []),
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
            arguments: dict[str, Any] = {
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
                "base": archive_base(None, []),
            }
            with self.assertRaises(PersistenceError):
                commit_adapter_result(**arguments)
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            archive.unlink()
            json_path, _, persisted = commit_adapter_result(**arguments)
            self.assertTrue(json_path.is_file())
            latest = latest_record(local, "example/one", 12)
            if latest is None:
                self.fail("the retried commit is in the local mirror")
            self.assertEqual(latest["artifacts"], persisted["artifacts"])

    @staticmethod
    def _pending_mirror_commit(root: Path) -> dict[str, Any]:
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
        arguments: dict[str, Any] = {
            "request_path": root / "request.json",
            "result_path": root / "result.json",
            "archive_root": root / "archive",
            "local_mirror_root": root / "local",
            "policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
            "base": archive_base(None, []),
        }
        arguments["request_path"].write_text(json.dumps(request), encoding="utf-8")
        arguments["result_path"].write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
        unrelated = build_record(
            valid_request(number=13), valid_adapter_result(number=13), version=1, policy=arguments["policy"]
        )
        for archive in (arguments["archive_root"], arguments["local_mirror_root"]):
            commit_record(archive, "example/one", 13, unrelated, expected_latest_version=None)
        commit_adapter_result(
            **{**arguments, "archive_root": arguments["local_mirror_root"], "local_mirror_root": None}
        )
        return arguments

    def _assert_mirror_refusal(self, arguments: dict[str, Any], message: str) -> None:
        roots = [arguments["archive_root"], arguments["local_mirror_root"]]
        before = [
            {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} for root in roots
        ]
        for files in before:
            self.assertIn(Path("example") / "one" / "pulls" / "13" / "review.json", files)
            self.assertIn(Path("example") / "one" / "pulls" / "13" / "review.md", files)
        with self.assertRaises(ReviewOperationError) as raised:
            commit_adapter_result(**arguments)
        self.assertIs(type(raised.exception), ReviewOperationError)
        self.assertEqual(message, str(raised.exception))
        for root, expected in zip(roots, before, strict=True):
            with self.subTest(archive=root.name):
                self.assertEqual(
                    expected,
                    {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()},
                )

    def test_mirror_refuses_a_reported_version_without_a_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            local = arguments["local_mirror_root"]
            directory = pull_directory(local, "example/one", 12)
            observed = list_versions(directory)
            self.assertEqual([1], observed)
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            (directory / "review.json").unlink()
            self.assertIsNone(latest_record(local, "example/one", 12))
            # The record disappeared after the version scan; only that stale observation is simulated.
            with mock.patch("review_operation.list_versions", return_value=observed) as versions:
                self._assert_mirror_refusal(arguments, "Local mirror reports a version without a valid record")
            versions.assert_called_once_with(directory)

    def test_mirror_refuses_a_different_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            result = valid_adapter_result()
            result["summary"] = "A different review of the same head."
            arguments["result_path"].write_text(json.dumps(result), encoding="utf-8")
            self._assert_mirror_refusal(
                arguments, "Local mirror contains a different pending review; reconcile before retrying"
            )

    def test_mirror_refuses_versions_that_differ_from_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            local = arguments["local_mirror_root"]
            record = build_record(valid_request(), valid_adapter_result(), version=2, policy=arguments["policy"])
            commit_record(local, "example/one", 12, record, expected_latest_version=1)
            self.assertEqual([1, 2], list_versions(pull_directory(local, "example/one", 12)))
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            self.assertEqual([], list_versions(pull_directory(arguments["archive_root"], "example/one", 12)))
            self._assert_mirror_refusal(
                arguments, "Local mirror and archive versions differ; reconcile before committing"
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


class RuntimeHostTests(GitTestCase):
    @staticmethod
    def _promote_fixture_result(staging: Path, result: Path) -> bool:
        """Rename a result in this test's owned temporary fixture."""
        staging.replace(result)
        return True

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
                    "coverage": {"unavailable_sources": []},
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
                    promote=self._promote_fixture_result,
                    diagnostic_path=run_directory / "diagnostic.jsonl",
                    isolation_root=run_directory / "isolation",
                    executable="copilot",
                )

    if sys.platform == "win32":

        def test_copilot_version_parser_and_winget_fallback(self) -> None:
            self.assertEqual((1, 0, 88), parse_copilot_version("GitHub Copilot CLI 1.0.88"))
            with tempfile.TemporaryDirectory() as temporary:
                local_app_data = Path(temporary)
                winget = local_app_data / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
                winget.parent.mkdir(parents=True)
                winget.write_bytes(b"fixture")
                with mock.patch.dict(
                    os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(winget.parent.parent)}
                ):
                    self.assertEqual(str(winget), find_copilot())
                on_path = local_app_data / "on path" / "copilot.exe"
                on_path.parent.mkdir()
                on_path.write_bytes(b"fixture")
                environment = {"LOCALAPPDATA": str(local_app_data), "PATH": str(on_path.parent), "PATHEXT": ".EXE"}
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(
                        str(on_path.with_suffix(".EXE")), find_copilot(), "the program on PATH comes first"
                    )

    else:

        def test_copilot_version_parser_and_path_lookup(self) -> None:
            self.assertEqual((1, 0, 88), parse_copilot_version("GitHub Copilot CLI 1.0.88"))
            with tempfile.TemporaryDirectory() as temporary:
                local_app_data = Path(temporary)
                winget = local_app_data / "Microsoft" / "WinGet" / "Links" / "copilot"
                winget.parent.mkdir(parents=True)
                winget.write_bytes(b"#!/bin/sh\n")
                winget.chmod(0o755)
                with (
                    mock.patch.dict(
                        os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(winget.parent.parent)}
                    ),
                    self.assertRaisesRegex(RuntimeContractError, "^GitHub Copilot CLI is not available$"),
                ):
                    find_copilot()  # no link folder is searched off Windows
                on_path = local_app_data / "on path" / "copilot"
                on_path.parent.mkdir()
                on_path.write_bytes(b"#!/bin/sh\n")
                on_path.chmod(0o755)
                with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(on_path.parent)}):
                    self.assertEqual(str(on_path), find_copilot(), "the program on PATH is found")

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
            invocations: list[tuple[list[str], Path, dict[str, str], str]] = []

            def runner(
                arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str
            ) -> ProcessResult:
                invocations.append((list(arguments), cwd, dict(environment), stdin))
                if "--version" in arguments:
                    return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                self.assertFalse(result_path.exists())
                staging.write_text('{"protocol_version": 1}', encoding="utf-8")
                return ProcessResult(0, '{"type":"assistant.message","text":"done"}\n', "")

            run_copilot(
                run_directory=root,
                materialized_root=materialized,
                request_path=request,
                result_path=result_path,
                staging_path=staging,
                promote=self._promote_fixture_result,
                diagnostic_path=diagnostic,
                isolation_root=isolation,
                runner=runner,
                executable="copilot",
                base_environment={"GH_TOKEN": "test-token"},
            )
            self.assertEqual(2, len(invocations))
            self.assertEqual((["copilot", "--version"], ""), (invocations[0][0], invocations[0][3]))
            self.assertIn("assistant.message", diagnostic.read_text(encoding="utf-8"))
            self.assertEqual({"protocol_version": 1}, json.loads(result_path.read_text()))
            self.assertFalse(staging.exists(), "the staging file is renamed into place, never copied")
            command, cwd, environment, prompt = invocations[-1]
            # Copilot may write only its staging file; the result appears whole, by rename, or not at all.
            self.assertIn(f"--allow-tool=write({staging.as_posix()})", command)
            self.assertNotIn(f"--allow-tool=write({result_path.as_posix()})", command)
            self.assertIn(str(staging), prompt)
            self.assertEqual(isolation / "workspace", cwd)
            self.assertEqual(str(isolation / "home"), environment["HOME"])
            self.assertEqual(str(isolation / "home"), environment["USERPROFILE"])
            self.assertEqual(str(isolation / "copilot-home"), environment["COPILOT_HOME"])
            self.assertEqual("test-token", environment["GH_TOKEN"])
            self.assertIn(f"REVIEWER_ROOT={materialized.resolve()}", prompt)
            self.assertIn(f"Follow REVIEWER_ROOT/{entrypoint.relative_to(materialized).as_posix()}", prompt)
            self.assertEqual([], [argument for argument in command if "REVIEWER_ROOT" in argument])

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
                    arguments: Sequence[str],
                    cwd: Path,
                    environment: Mapping[str, str],
                    stdin: str,
                    *,
                    case: str = case,
                    staging: Path = staging,
                ) -> ProcessResult:
                    del cwd, environment, stdin
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
                    promote=self._promote_fixture_result,
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
                    promote=self._promote_fixture_result,
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

            def runner(
                arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str
            ) -> ProcessResult:
                del cwd, environment, stdin
                self.assertIn("--version", arguments)
                return ProcessResult(0, "GitHub Copilot CLI 1.0.87\n", "")

            with self.assertRaisesRegex(RuntimeContractError, "1.0.88 or newer"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
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
                    arguments: Sequence[str],
                    cwd: Path,
                    environment: Mapping[str, str],
                    stdin: str,
                    *,
                    timed_out: str = timed_out,
                    partial: bytes | None = partial,
                ) -> ProcessResult:
                    del cwd, environment, stdin
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
                        promote=self._promote_fixture_result,
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
                    promote=self._promote_fixture_result,
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
                    promote=self._promote_fixture_result,
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
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=isolation,
                    executable="copilot",
                )


if __name__ == "__main__":
    unittest.main()

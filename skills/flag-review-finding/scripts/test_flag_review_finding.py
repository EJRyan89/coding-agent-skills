from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))

import review_flags
import test_review_fixture as review_fixture
from flag_review_finding import flag_line, main
from review_archive import ArchiveError, ArchiveFieldError, pull_directory
from review_config import write_config
from review_io import PersistenceError, ResourceLock
from typed_fields import FieldError

SCRIPT = Path(__file__).with_name("flag_review_finding.py")


class FlagLineTests(unittest.TestCase):
    def flag(self, **fields: object) -> dict[str, object]:
        return {
            "id": "flag-1",
            "category": "false positive",
            "repository": "example/one",
            "pull_number": 12,
            "review_version": 2,
            "finding_id": "F1",
            "body": "Too noisy.",
            **fields,
        }

    def test_a_flag_is_one_line_naming_what_it_points_at(self) -> None:
        self.assertEqual("FLAG flag-1 false positive example/one#12 v2 F1 Too noisy.", flag_line(self.flag()))
        spread = self.flag(category="missed\nbug", review_version=None, finding_id=None, body="the\tbody\nspans  lines")
        self.assertEqual("FLAG flag-1 missed bug example/one#12 the body spans lines", flag_line(spread))
        unpointed = self.flag(repository=None, pull_number=None, review_version=None, finding_id=None)
        self.assertEqual("FLAG flag-1 false positive - Too noisy.", flag_line(unpointed))

    def test_a_long_body_is_cut_to_the_limit_with_an_ellipsis(self) -> None:
        cut = flag_line(self.flag(body="word " * 100))
        body = cut.split(" F1 ", 1)[1]
        self.assertEqual(160, len(body))
        self.assertTrue(body.endswith("word…"))


class ArchivePullTests(unittest.TestCase):
    """The archive folder a flag's pull request is looked up in, named by its pull number."""

    def test_a_pull_number_of_the_wrong_kind_is_refused_in_one_shape(self) -> None:
        self.assertTrue(issubclass(ArchiveFieldError, FieldError) and issubclass(ArchiveFieldError, ArchiveError))
        root = Path("C:/archive")
        self.assertEqual(root / "example" / "one" / "pulls" / "12", pull_directory(root, "example/one", 12))
        kind = "pull number must be an integer of at least 1, found"
        cases: list[tuple[Any, str]] = [
            (0, "integer 0"),
            (-1, "integer -1"),
            (True, "boolean true"),
            (12.0, "number 12.0"),
            ("12", 'string "12"'),
            (None, "null"),
            ([12], "an array"),
            ({"number": 12}, "an object"),
        ]
        for value, found in cases:
            with self.subTest(value=value):
                with self.assertRaises(ArchiveError) as caught:
                    pull_directory(root, "example/one", value)
                self.assertEqual(
                    (ArchiveFieldError, f"{kind} {found}"), (type(caught.exception), str(caught.exception))
                )


class FlagCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name) / "root with spaces"
        self.store = str(root / "flags.json")
        self.archive = root / "archive"
        self.config = str(root / "config.json")
        reviewer = {
            "reviewer": {
                "id": "generic",
                "protocol_version": 1,
                "trusted_ref": None,
                "scope": "generic",
                "manifest_path": None,
            },
            "checkout_path": None,
        }
        write_config(
            {
                "schema_version": 1,
                "default_repository_set": "primary",
                "repository_sets": {"primary": ["example/one"]},
                "repositories": {"example/one": reviewer},
                "operation_repository_sets": {},
                "archive_root": str(self.archive),
                "local_mirror_root": None,
                "summary_root": str(root / "summary"),
                "dashboard_file": str(root / "dashboard.md"),
                "github_login": "reviewer",
            },
            Path(self.config),
        )

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
        return self.run_cli("--store", self.store, "--config", self.config, *arguments)

    def flag_finding(self, version: str | None, finding: str) -> subprocess.CompletedProcess[str]:
        """Add a flag on a finding of example/one#12, naming the review version unless it is None."""
        named_version = () if version is None else ("--review-version", version)
        return self.store_cli(
            "add",
            "guideline",
            "Clarify boundary",
            "--repository",
            "example/one",
            "--pull",
            "12",
            *named_version,
            "--finding",
            finding,
        )

    def stored_targets(self) -> list[tuple[Any, ...]]:
        """The repository, pull request, review version, and finding of each stored flag."""
        flags = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"]
        fields = ("repository", "pull_number", "review_version", "finding_id")
        return [tuple(flag[field] for field in fields) for flag in flags]

    def assert_result(self, result: subprocess.CompletedProcess[str], code: int, stdout: str) -> None:
        self.assertEqual((code, stdout, ""), (result.returncode, result.stdout, result.stderr))

    def assert_refusal_preserves_store_and_locks(self, reason: str, *arguments: str) -> None:
        root = Path(self.store).parent

        def snapshot() -> dict[Path, tuple[bytes | None, int]]:
            return {
                path.relative_to(root): (path.read_bytes() if path.is_file() else None, path.stat().st_mtime_ns)
                for path in (root, *root.rglob("*"))
            }

        before = snapshot()
        self.assert_result(self.store_cli(*arguments), 1, f"FAILED {reason}\n")
        self.assertEqual(before, snapshot())

    def test_add_requires_a_nonblank_category_without_changing_the_store(self) -> None:
        for existing in (False, True):
            if existing:
                self.assert_result(self.store_cli("add", "guideline", "Body"), 0, "ADDED RF-000001\n")
            for category in ("", " \t\n"):
                with self.subTest(existing=existing, category=category):
                    self.assert_refusal_preserves_store_and_locks(
                        "Flag category and body are required", "add", category, "Body"
                    )

    def test_add_requires_a_nonblank_body_without_changing_the_store(self) -> None:
        for existing in (False, True):
            if existing:
                self.assert_result(self.store_cli("add", "guideline", "Body"), 0, "ADDED RF-000001\n")
            for body in ("", " \t\n"):
                with self.subTest(existing=existing, body=body):
                    self.assert_refusal_preserves_store_and_locks(
                        "Flag category and body are required", "add", "guideline", body
                    )

    def test_add_requires_a_positive_pull_before_reading_the_archive_or_changing_the_store(self) -> None:
        for existing in (False, True):
            if existing:
                self.assert_result(
                    self.store_cli("add", "guideline", "Body", "--repository", "example/one", "--pull", "1"),
                    0,
                    "ADDED RF-000001\n",
                )
            for pull in ("0", "-1"):
                for finding in (False, True):
                    with self.subTest(existing=existing, pull=pull, finding=finding):
                        arguments = ["add", "guideline", "Body", "--repository", "example/one", "--pull", pull]
                        if finding:
                            arguments.extend(["--review-version", "1", "--finding", "F001"])
                        self.assert_refusal_preserves_store_and_locks("Pull number must be positive", *arguments)

    def test_blank_resolution_fails_before_creating_a_store_or_lock(self) -> None:
        for resolution in ("", " \t\n"):
            with self.subTest(resolution=resolution):
                self.assert_refusal_preserves_store_and_locks(
                    "Resolution is required", "resolve", "RF-000001", resolution
                )

    def test_add_list_and_resolve(self) -> None:
        review_fixture.commit_fixture(self.archive)
        self.assert_result(self.flag_finding("3", "F002"), 0, "ADDED RF-000001\n")
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual((3, "F002"), (stored["review_version"], stored["finding_id"]))
        self.assert_result(
            self.store_cli("list"), 0, "FLAG RF-000001 guideline example/one#12 v3 F002 Clarify boundary\nCOUNT 1\n"
        )
        self.assert_result(self.store_cli("resolve", "RF-000001", "Accepted"), 0, "RESOLVED RF-000001\n")
        self.assert_result(self.store_cli("list"), 0, "COUNT 0\n")

    def test_edit_changes_the_category_and_body_of_an_open_flag(self) -> None:
        review_fixture.commit_fixture(self.archive)
        self.assert_result(self.flag_finding("3", "F002"), 0, "ADDED RF-000001\n")
        before = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assert_result(
            self.store_cli("edit", "RF-000001", "--category", "noise", "--body", "Caller already guarantees it"),
            0,
            "EDITED RF-000001\n",
        )
        self.assert_result(
            self.store_cli("edit", "RF-000001", "--body", "Rationale with spaces"), 0, "EDITED RF-000001\n"
        )
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual({**before, "category": "noise", "body": "Rationale with spaces"}, stored)
        self.assert_result(
            self.store_cli("list"), 0, "FLAG RF-000001 noise example/one#12 v3 F002 Rationale with spaces\nCOUNT 1\n"
        )

    def test_edit_refusals_print_their_reason_and_leave_the_store_and_locks_unchanged(self) -> None:
        self.assert_result(self.store_cli("add", "guideline", "Open"), 0, "ADDED RF-000001\n")
        self.assert_result(self.store_cli("add", "guideline", "Resolved"), 0, "ADDED RF-000002\n")
        self.assert_result(self.store_cli("resolve", "RF-000002", "Done"), 0, "RESOLVED RF-000002\n")
        # Refused under the store lock, which leaves the store as it was.
        for reason, arguments in (
            ("Flag RF-000002 is resolved; only an open flag is edited", ("RF-000002", "--body", "New")),
            ("Unknown flag ID: RF-000009", ("RF-000009", "--category", "noise")),
        ):
            with self.subTest(arguments=arguments):
                before = Path(self.store).read_bytes()
                self.assert_result(self.store_cli("edit", *arguments), 1, f"FAILED {reason}\n")
                self.assertEqual(before, Path(self.store).read_bytes())
        # Refused before the lock is taken, which leaves the lock folder as it was too.
        unlocked: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("Flag category must not be blank", ("RF-000001", "--category", " \t")),
            ("Flag body must not be blank", ("RF-000001", "--body", "")),
            ("Name a new category, a new body, or both", ("RF-000001",)),
        )
        for reason, unlocked_arguments in unlocked:
            with self.subTest(arguments=unlocked_arguments):
                self.assert_refusal_preserves_store_and_locks(reason, "edit", *unlocked_arguments)

    def test_resolving_a_resolved_flag_keeps_its_resolution(self) -> None:
        self.store_cli("add", "guideline", "Body")
        self.store_cli("resolve", "RF-000001", "First")
        before = Path(self.store).read_bytes()
        self.assert_result(self.store_cli("resolve", "RF-000001", "Second"), 0, "ALREADY_RESOLVED RF-000001\n")
        self.assertEqual(before, Path(self.store).read_bytes())
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual("First", stored["resolution"])

    def test_a_resolution_won_before_lock_acquisition_prints_already_resolved_and_keeps_the_store(self) -> None:
        self.assert_result(self.store_cli("add", "guideline", "Body"), 0, "ADDED RF-000001\n")
        store = Path(self.store)
        winning_store: list[bytes] = []

        def before_lock(lock: Path) -> ResourceLock:
            self.assertEqual(store.parent / ".locks" / "flags.lock", lock)
            with mock.patch.object(review_flags, "ResourceLock", ResourceLock):
                review_flags.resolve_flag(store, "RF-000001", "First")
            winning_store.append(store.read_bytes())
            return ResourceLock(lock)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(review_flags, "ResourceLock", side_effect=before_lock),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["--store", self.store, "resolve", "RF-000001", "Second"])
        self.assertEqual((0, "ALREADY_RESOLVED RF-000001\n", ""), (code, stdout.getvalue(), stderr.getvalue()))
        self.assertEqual([store.read_bytes()], winning_store)
        self.assertEqual("First", json.loads(store.read_bytes())["flags"][0]["resolution"])

    def test_resolve_requires_a_nonempty_resolution_even_when_already_resolved(self) -> None:
        self.store_cli("add", "guideline", "Body")
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                if resolved:
                    self.store_cli("resolve", "RF-000001", "First")
                before = Path(self.store).read_bytes()
                self.assert_result(self.store_cli("resolve", "RF-000001", " \t"), 1, "FAILED Resolution is required\n")
                self.assertEqual(before, Path(self.store).read_bytes())

    def test_resolve_store_failures_are_not_classified_as_already_resolved(self) -> None:
        self.store_cli("add", "guideline", "Body")
        before = Path(self.store).read_bytes()
        for operation, error in (
            ("ResourceLock", PersistenceError("The store is locked")),
            ("load_store", review_flags.FlagError("Flag is already resolved: RF-000001")),
            ("atomic_write_json", OSError("Write failed")),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(operation=operation),
                mock.patch.object(review_flags, operation, side_effect=error),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = main(["--store", self.store, "resolve", "RF-000001", "Done"])
            self.assertEqual((1, f"FAILED {error}\n", ""), (code, stdout.getvalue(), stderr.getvalue()))
            self.assertEqual(before, Path(self.store).read_bytes())

    def test_a_failure_spread_over_lines_is_one_failed_line(self) -> None:
        self.store_cli("add", "guideline", "Body")
        error = PersistenceError("The store\n\tcannot be\u00a0written:  \r\n disk full\n")
        stdout = io.StringIO()
        with (
            mock.patch.object(review_flags, "ResourceLock", side_effect=error),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["--store", self.store, "resolve", "RF-000001", "Done"])
        self.assertEqual((1, "FAILED The store cannot be written: disk full\n"), (code, stdout.getvalue()))

    def test_a_category_and_body_with_odd_whitespace_print_on_one_line_and_the_body_is_cut(self) -> None:
        self.store_cli("add", "mis\n\tsed\u00a0bug", "Line one\x0bline\x1ftwo\u2003three " + "word " * 50)
        result = self.store_cli("list")
        self.assert_result(
            result,
            0,
            "FLAG RF-000001 mis sed bug - Line one line two three " + " ".join(["word"] * 27) + "…\nCOUNT 1\n",
        )

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
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(expected, result.stdout.decode("utf-8").replace("\r\n", "\n"))

    def test_store_defaults_to_the_core_flag_path(self) -> None:
        store = Path(self._temporary.name) / "flag store" / "flags.json"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "add", "guideline", "Body"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "CODE_REVIEW_FLAGS": str(store)},
        )
        self.assert_result(result, 0, "ADDED RF-000001\n")
        self.assertEqual("RF-000001", json.loads(store.read_text(encoding="utf-8"))["flags"][0]["id"])

    def test_a_finding_without_its_review_version_fails(self) -> None:
        result = self.store_cli(
            "add",
            "guideline",
            "Body",
            "--repository",
            "example/one",
            "--pull",
            "12",
            "--finding",
            "F001",
        )
        self.assert_result(
            result,
            1,
            "FAILED A flag that names a finding must name its repository, pull request, and review version\n",
        )
        self.assertFalse(Path(self.store).exists())

    def test_a_finding_that_is_not_a_finding_id_fails_before_the_archive_is_read(self) -> None:
        # No review is archived, so a refusal that names the form was decided before the archive was read.
        for version, finding in (
            ("2", "f2"),
            ("2", "garbage"),
            ("2", "F01"),
            ("2", ""),
            (None, "v2F002"),
            (None, "v2:F002"),
            (None, "v0 F002"),
            (None, "v2 F02"),
            (None, " v2 F002"),
            (None, "v2 F002 F003"),
        ):
            with self.subTest(finding=finding):
                self.assert_result(
                    self.flag_finding(version, finding),
                    1,
                    f"FAILED Finding ID {finding!r} is not a finding ID such as F001; a report label such as "
                    "v2 F003 names review version 2 and finding F003\n",
                )
        self.assertFalse(Path(self.store).exists())

    def test_a_finding_the_archive_lacks_fails(self) -> None:
        review_fixture.commit_fixture(self.archive)
        for version, finding, reason in (
            ("2", "F001", "Review v2 of example/one#12 has no finding F001"),
            ("3", "F009", "Review v3 of example/one#12 has no finding F009"),
            ("4", "F001", "example/one#12 has no review v4 in the archive"),
        ):
            with self.subTest(version=version, finding=finding):
                self.assert_result(self.flag_finding(version, finding), 1, f"FAILED {reason}\n")
        self.assertFalse(Path(self.store).exists())

    def test_a_report_label_the_archive_lacks_fails_as_its_parts_do(self) -> None:
        review_fixture.commit_fixture(self.archive)
        for label, reason in (
            ("v2 F001", "Review v2 of example/one#12 has no finding F001"),
            ("v3 F009", "Review v3 of example/one#12 has no finding F009"),
            ("v4 F001", "example/one#12 has no review v4 in the archive"),
        ):
            with self.subTest(label=label):
                self.assert_result(self.flag_finding(None, label), 1, f"FAILED {reason}\n")
        self.assertFalse(Path(self.store).exists())

    def test_a_report_label_beside_a_review_version_it_does_not_name_fails_before_the_archive_is_read(self) -> None:
        self.assert_result(
            self.flag_finding("2", "v3 F002"), 1, "FAILED Report label 'v3 F002' names review version 3, not 2\n"
        )
        self.assertFalse(Path(self.store).exists())

    def test_a_report_label_names_what_its_split_form_names(self) -> None:
        review_fixture.commit_fixture(self.archive)
        self.assert_result(self.flag_finding(None, "v3 F002"), 0, "ADDED RF-000001\n")
        self.assert_result(self.flag_finding("3", "v3 F002"), 0, "ADDED RF-000002\n")
        self.assert_result(self.flag_finding("3", "F002"), 0, "ADDED RF-000003\n")
        self.assertEqual([("example/one", 12, 3, "F002")] * 3, self.stored_targets())

    def test_a_flag_on_a_finding_of_an_unreviewed_pull_request_fails(self) -> None:
        self.assert_result(self.flag_finding("1", "F001"), 1, "FAILED example/one#12 has no review v1 in the archive\n")

    def test_findings_lists_the_open_findings_of_a_pull_request_by_report_label(self) -> None:
        # v1 F001 is open and was repeated as v3 F001, so it is shown where it was last reported; v1 F002 was
        # addressed in v2; v3 F002 is new.
        review_fixture.commit_fixture(self.archive)
        self.assert_result(
            self.store_cli("findings", "--repository", "example/one", "--pull", "12"),
            0,
            "FINDING v1 F001 MUST_FIX src/lock.py:14 Lock leaks on the timeout path\n"
            "FINDING v3 F002 SUGGESTION src/retry.py:5 Name the retry limit\n"
            "COUNT 2\n",
        )
        self.assert_result(self.flag_finding("1", "F001"), 0, "ADDED RF-000001\n")

    def test_add_takes_each_finding_under_the_label_findings_prints(self) -> None:
        review_fixture.commit_fixture(self.archive)
        printed = self.store_cli("findings", "--repository", "example/one", "--pull", "12").stdout.splitlines()
        labels = [" ".join(line.split()[1:3]) for line in printed if line.startswith("FINDING ")]
        self.assertEqual(["v1 F001", "v3 F002"], labels)
        for number, label in enumerate(labels, 1):
            self.assert_result(self.flag_finding(None, label), 0, f"ADDED RF-{number:06d}\n")
        self.assertEqual([("example/one", 12, 1, "F001"), ("example/one", 12, 3, "F002")], self.stored_targets())

    def test_findings_of_an_unreviewed_pull_request_fail(self) -> None:
        self.assert_result(
            self.store_cli("findings", "--repository", "example/one", "--pull", "12"),
            1,
            "FAILED example/one#12 has no review in the archive\n",
        )

    def assert_one_failed_line_and_no_store(self, result: subprocess.CompletedProcess[str], pattern: str) -> None:
        self.assertEqual((1, ""), (result.returncode, result.stderr))
        self.assertRegex(result.stdout, rf"\AFAILED {pattern}\n\Z")
        self.assertEqual([], [path.name for path in Path(self.store).parent.glob("flags.json*")])

    def test_a_review_whose_markdown_changed_after_it_was_archived_fails_both_reads(self) -> None:
        review_fixture.commit_fixture(self.archive)
        markdown = pull_directory(self.archive, "example/one", 12) / "review.md"
        markdown.write_bytes(markdown.read_bytes() + b"\nEdited after the review.\n")
        self.assert_refusal_preserves_store_and_locks(
            "Review Markdown hash mismatch",
            "add",
            "guideline",
            "Clarify boundary",
            "--repository",
            "example/one",
            "--pull",
            "12",
            "--review-version",
            "1",
            "--finding",
            "F001",
        )
        self.assert_refusal_preserves_store_and_locks(
            "Review Markdown hash mismatch", "findings", "--repository", "example/one", "--pull", "12"
        )
        self.assertFalse(Path(self.store).exists())

    def test_a_review_whose_markdown_is_missing_fails_both_reads(self) -> None:
        review_fixture.commit_fixture(self.archive)
        (pull_directory(self.archive, "example/one", 12) / "review.md").unlink()
        # The reason is the operating system's, never a failure's type name, which only an uncaught error prints.
        missing = r"\[Errno 2\] [^\n]*review\.md'?"
        self.assert_one_failed_line_and_no_store(self.flag_finding("1", "F001"), missing)
        self.assert_one_failed_line_and_no_store(
            self.store_cli("findings", "--repository", "example/one", "--pull", "12"), missing
        )

    def test_a_configuration_that_cannot_be_read_fails_both_reads(self) -> None:
        review_fixture.commit_fixture(self.archive)
        missing = str(Path(self.config).with_name("absent.json"))
        pull = ("--repository", "example/one", "--pull", "12")
        commands = (
            ("findings", *pull),
            ("add", "guideline", "Body", *pull, "--review-version", "1", "--finding", "F001"),
        )
        for config in (missing, self.config):
            if config == self.config:
                Path(self.config).write_text("{not json", encoding="utf-8")
            for command in commands:
                with self.subTest(config=config, command=command[0]):
                    result = self.run_cli("--store", self.store, "--config", config, *command)
                    self.assert_one_failed_line_and_no_store(result, r"Cannot read valid JSON from [^\n]+")

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

    def test_a_store_whose_finding_id_is_a_list_fails(self) -> None:
        self.store_cli("add", "guideline", "Body")
        store = json.loads(Path(self.store).read_text(encoding="utf-8"))
        store["flags"][0].update(repository="example/one", pull_number=12, review_version=1, finding_id=["F001"])
        Path(self.store).write_text(json.dumps(store), encoding="utf-8")
        for arguments in (["list"], ["resolve", "RF-000001", "Done"], ["add", "guideline", "Body"]):
            with self.subTest(arguments=arguments):
                self.assert_result(
                    self.store_cli(*arguments),
                    1,
                    "FAILED flag-store.flags[0].finding_id must be a string or null, found an array\n",
                )

    def test_findings_print_a_path_spread_over_lines_on_one_line(self) -> None:
        versions = copy.deepcopy(review_fixture.VERSIONS)
        first = versions[0]["findings"][0]
        path = "src/a\u2028b\u2029c\x85d\re\nADDED RF-999999.py"
        first.update(path=path, evidence=f"{path}:10 adds: lock.acquire()")
        with mock.patch.object(review_fixture, "VERSIONS", versions):
            review_fixture.commit_fixture(self.archive, versions=1)
        self.assert_result(
            self.store_cli("findings", "--repository", "example/one", "--pull", "12"),
            0,
            "FINDING v1 F001 MUST_FIX src/a b c d e ADDED RF-999999.py:10 Lock is never released\n"
            "FINDING v1 F002 SHOULD_FIX src/parse.py:20 Null result is not checked\n"
            "COUNT 2\n",
        )

    def test_a_category_or_body_past_its_limit_is_refused_before_the_lock(self) -> None:
        self.assert_result(self.store_cli("add", "c" * 120, "b" * 4000), 0, "ADDED RF-000001\n")
        self.assert_result(
            self.store_cli("edit", "RF-000001", "--category", "d" * 120, "--body", "e" * 4000), 0, "EDITED RF-000001\n"
        )
        stored = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]
        self.assertEqual(("d" * 120, "e" * 4000), (stored["category"], stored["body"]))
        category = "Flag category holds 121 characters; the most it holds is 120"
        body = "Flag body holds 4001 characters; the most it holds is 4000"
        for reason, arguments in (
            (category, ("add", "c" * 121, "Body")),
            (body, ("add", "guideline", "b" * 4001)),
            (category, ("edit", "RF-000001", "--category", "c" * 121)),
            (body, ("edit", "RF-000001", "--body", "b" * 4001)),
        ):
            with self.subTest(reason=reason, command=arguments[0]):
                self.assert_refusal_preserves_store_and_locks(reason, *arguments)

    def write_store_of_size(self, size: int) -> None:
        """A store of open flags, RF-000001 first with the body "a", whose file is `size` bytes as the store writes it.
        It is padded through RF-000002's body, which grows the file a byte a character."""

        def flag(number: int, body: str) -> dict[str, Any]:
            return {
                "id": f"RF-{number:06d}",
                "status": "open",
                "created_at": "2026-10-15T12:00:00+00:00",
                "resolved_at": None,
                "repository": None,
                "pull_number": None,
                "review_version": None,
                "finding_id": None,
                "category": "noise",
                "body": body,
                "resolution": None,
            }

        def serialized(store: dict[str, Any]) -> bytes:
            return (json.dumps(store, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")

        flags = [flag(1, "a"), flag(2, "")]
        flags.extend(flag(number, "x" * 4000) for number in range(3, 3 + size // 4500))
        store = {"schema_version": 2, "next_id": len(flags) + 1, "flags": flags}
        flags[1]["body"] = "p" * (size - len(serialized(store)))
        Path(self.store).parent.mkdir(parents=True, exist_ok=True)
        Path(self.store).write_bytes(serialized(store))
        self.assertEqual(size, Path(self.store).stat().st_size)

    def test_a_change_that_takes_the_store_past_four_mebibytes_is_refused_and_the_store_still_reads(self) -> None:
        self.write_store_of_size(4194304 - 3)
        # Three more characters in a body make the store exactly as large as it may be.
        self.assert_result(self.store_cli("edit", "RF-000001", "--body", "abcd"), 0, "EDITED RF-000001\n")
        self.assertEqual(4194304, Path(self.store).stat().st_size)
        self.assertEqual("abcd", json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"][0]["body"])
        self.assertRegex(self.store_cli("list").stdout, r"\nCOUNT \d+\n\Z")
        # Refused under the store lock, which leaves the store as it was.
        reason = "The flag store would exceed 4194304 bytes, the most a flag store is read at"
        before = Path(self.store).read_bytes()
        for arguments in (
            ("edit", "RF-000001", "--body", "abcde"),
            ("add", "noise", "Body"),
            ("resolve", "RF-000001", "Done"),
        ):
            with self.subTest(command=arguments[0]):
                self.assert_result(self.store_cli(*arguments), 1, f"FAILED {reason}\n")
                self.assertEqual(before, Path(self.store).read_bytes())
        self.write_store_of_size(4194304 + 1)
        listed = self.store_cli("list")
        self.assertEqual((1, ""), (listed.returncode, listed.stderr))
        self.assertIn("exceeds 4194304 bytes", listed.stdout)

    def test_add_refuses_a_pull_without_its_repository_and_a_review_version_without_its_finding(self) -> None:
        pull = "A flag that names a pull request must name its repository"
        version = "A flag that names a review version must name the finding in it"
        for existing in (False, True):
            if existing:
                self.assert_result(self.store_cli("add", "guideline", "Body"), 0, "ADDED RF-000001\n")
            for reason, options in (
                (pull, ("--pull", "7")),
                (version, ("--repository", "example/one", "--pull", "7", "--review-version", "3")),
                (version, ("--pull", "7", "--review-version", "3")),
            ):
                with self.subTest(existing=existing, options=options):
                    self.assert_refusal_preserves_store_and_locks(reason, "add", "noise", "Body", *options)
        self.assertEqual(1, len(json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"]))

    @staticmethod
    def skill_command(command: str, values: dict[str, str]) -> list[str]:
        """The arguments of SKILL.md's `command` command, each placeholder replaced by its value in `values`."""
        text = SCRIPT.parents[1].joinpath("SKILL.md").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if f'flag_review_finding.py" {command} ' in line]
        if len(lines) != 1:
            raise AssertionError(f"SKILL.md has {len(lines)} {command} commands")
        arguments = shlex.split(lines[0].split('flag_review_finding.py" ', 1)[1])
        for placeholder, value in values.items():
            arguments = [argument.replace(placeholder, value) for argument in arguments]
        return arguments

    def test_values_that_begin_with_a_dash_are_taken_in_the_skill_command_forms(self) -> None:
        review_fixture.commit_fixture(self.archive)
        target = {"<owner/repo>": "example/one", "<number>": "12", "<label>": "v3 F002"}
        add = self.skill_command("add", {**target, "<category>": "-noise", "<rationale>": "--verbose"})
        self.assert_result(self.store_cli(*add), 0, "ADDED RF-000001\n")
        # Without a finding, the options before -- are left out.
        add = self.skill_command("add", {"<category>": "-x", "<rationale>": "-y"})
        self.assert_result(self.store_cli(add[0], *add[add.index("--") :]), 0, "ADDED RF-000002\n")
        edit = self.skill_command(
            "edit", {"<flag id>": "RF-000002", "<category>": "-category", "<rationale>": "--body"}
        )
        self.assert_result(self.store_cli(*edit), 0, "EDITED RF-000002\n")
        resolve = self.skill_command("resolve", {"<flag id>": "RF-000001", "<resolution>": "-done"})
        self.assert_result(self.store_cli(*resolve), 0, "RESOLVED RF-000001\n")
        first, second = json.loads(Path(self.store).read_text(encoding="utf-8"))["flags"]
        fields = ("repository", "pull_number", "review_version", "finding_id", "category", "body", "resolution")
        self.assertEqual(
            ("example/one", 12, 3, "F002", "-noise", "--verbose", "-done"), tuple(first[field] for field in fields)
        )
        self.assertEqual(
            (None, None, None, None, "-category", "--body", None), tuple(second[field] for field in fields)
        )

    def test_refusing_a_flag_id_without_a_store_creates_nothing(self) -> None:
        folder = Path(self._temporary.name) / "new folder"
        store = str(folder / "flags.json")
        for arguments in (("resolve", "RF-000001", "Done"), ("edit", "RF-000001", "--body", "x")):
            with self.subTest(command=arguments[0]):
                self.assert_result(self.run_cli("--store", store, *arguments), 1, "FAILED Unknown flag ID: RF-000001\n")
                self.assertFalse(folder.exists())

    def test_usage_errors_exit_2(self) -> None:
        for arguments in (
            ["add", "guideline"],
            ["add", "guideline", "Body", "--pull", "twelve"],
            ["remove"],
            ["findings", "--repository", "example/one"],
        ):
            with self.subTest(arguments=arguments):
                result = self.store_cli(*arguments)
                self.assertEqual((2, ""), (result.returncode, result.stdout))
                self.assertIn("usage:", result.stderr)

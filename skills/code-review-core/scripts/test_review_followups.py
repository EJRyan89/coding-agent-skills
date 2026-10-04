from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import review_operation  # noqa: E402
import review_runtime  # noqa: E402
from review_config import (  # noqa: E402
    ConfigurationError,
    resolve_repositories,
    selected_repository_set,
    validate_config,
)
from review_runtime import RuntimeContractError  # noqa: E402

HEAD = "c" * 40


def tarball(members: dict[str, bytes], *, prefix: str = "owner-repo-ccc/") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class GitHubSnapshotTests(unittest.TestCase):
    def snapshot(self, members: dict[str, bytes], changed: tuple[str, ...] = ()) -> tuple[Path, dict]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name).resolve() / "source"
        data = tarball(members)
        metadata = review_runtime.materialize_source_snapshot_from_github(
            "owner/repo", HEAD, destination,
            fetcher=lambda repository, commit, target: target.write_bytes(data),
            changed_paths=changed,
        )
        return destination, metadata

    def test_tarball_snapshot_strips_top_folder_and_records_exclusions(self) -> None:
        big = b"x" * (review_runtime.MAX_SOURCE_FILE_BYTES + 1)
        destination, metadata = self.snapshot({
            "src/A.cs": b"class A {}\n",
            "img.png": b"\x89PNG\0",
            "C:../escape.txt": b"x",
            "data/a:b.txt": b"x",
            "CLAUDE.md": b"instructions",
            "db/Changed.sql": big,
            "db/Context.sql": big,
        }, changed=("db/Changed.sql",))
        self.assertTrue((destination / "src/A.cs").is_file())
        self.assertTrue((destination / "db/Changed.sql").is_file())
        excluded = metadata["excluded_paths"]
        self.assertEqual("binary", excluded["img.png"])
        self.assertEqual("unsafe-path", excluded["C:../escape.txt"])
        self.assertEqual("unsafe-path", excluded["data/a:b.txt"])
        self.assertEqual("agent-instruction", excluded["CLAUDE.md"])
        self.assertEqual("file-size-limit", excluded["db/Context.sql"])
        self.assertEqual([], list(destination.parent.rglob("escape.txt")))
        review_runtime.verify_source_snapshot(destination, expected_repository="owner/repo", expected_commit=HEAD)

    def test_case_collisions_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeContractError, "case-insensitive"):
            self.snapshot({"src/A.cs": b"one\n", "SRC/a.cs": b"two\n"})

    def test_fetch_failure_leaves_no_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source"

            def failing(repository: str, commit: str, target: Path) -> None:
                raise RuntimeContractError("boom")

            with self.assertRaisesRegex(RuntimeContractError, "boom"):
                review_runtime.materialize_source_snapshot_from_github("owner/repo", HEAD, destination, fetcher=failing)
            self.assertFalse(destination.exists())


class ConfigOperationSetTests(unittest.TestCase):
    def config(self, **extra: object) -> dict:
        generic = {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}, "checkout_path": None}
        value = {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["owner/one"], "tracked": ["owner/one", "owner/two"]},
            "repositories": {"owner/one": generic, "owner/two": generic},
            "archive_root": "C:/A", "summary_root": "C:/S", "dashboard_file": "C:/D.md",
        }
        value.update(extra)
        return validate_config(value)

    def test_operation_set_overrides_default_but_not_explicit_choices(self) -> None:
        config = self.config(operation_repository_sets={"update-pr-tracker": "tracked"})
        self.assertEqual(["owner/one", "owner/two"], resolve_repositories(config, operation="update-pr-tracker"))
        self.assertEqual(["owner/one"], resolve_repositories(config, operation="review-prs"))
        self.assertEqual(["owner/one"], resolve_repositories(config, operation="update-pr-tracker", repository_set="primary"))
        self.assertEqual(["owner/two"], resolve_repositories(config, explicit=["owner/two"], operation="update-pr-tracker"))
        self.assertEqual("tracked", selected_repository_set(config, operation="update-pr-tracker"))
        self.assertEqual("primary", selected_repository_set(config, operation="review-insights"))
        self.assertEqual("tracked", selected_repository_set(config, repository_set="tracked", operation="review-prs"))

    def test_invalid_operation_sets_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown operation"):
            self.config(operation_repository_sets={"review-everything": "tracked"})
        with self.assertRaisesRegex(ConfigurationError, "existing set"):
            self.config(operation_repository_sets={"review-prs": "missing"})


class ReviewedHeadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "owner" / "repo" / "pulls" / "5"
        self.directory.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_legacy(self, **overrides: object) -> None:
        value = {
            "schema_version": 1, "kind": "legacy-review-index", "repository": "owner/repo", "pull_number": 5,
            "reviewed_at": "2026-01-01T00:00:00+00:00", "reviewed_head_sha": "a" * 40, "verdict": "APPROVED",
            "source_sha256": "0" * 64, "source_path": "C:/legacy/review-5.md", "source_file_sha256": "0" * 64,
        }
        value.update(overrides)
        (self.directory / "legacy-review.json").write_text(json.dumps(value), encoding="utf-8")

    def test_legacy_index_counts_as_reviewed(self) -> None:
        self.assertIsNone(review_operation.reviewed_head(self.root, "owner/repo", 5))
        self.write_legacy()
        self.assertEqual({"head_sha": "a" * 40, "source": "legacy", "version": None, "incomplete": False, "verdict": "APPROVED", "counts": None, "report": None},
                         review_operation.reviewed_head(self.root, "owner/repo", 5))
        self.assertEqual({5: "a" * 40}, review_operation.latest_reviewed_heads(self.root, "owner/repo", [5, 6]))

    def test_legacy_counts_and_report_come_from_the_migrated_report(self) -> None:
        self.write_legacy()
        (self.directory / "legacy-review.md").write_text(
            "<summary><strong>MUST FIX (1)</strong></summary>\n<summary><strong>SUGGESTIONS (3)</strong></summary>\n",
            encoding="utf-8")
        reviewed = review_operation.reviewed_head(self.root, "owner/repo", 5)
        self.assertEqual({"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 3}, reviewed["counts"])
        self.assertEqual(str(self.directory / "legacy-review.md"), reviewed["report"])

    def test_invalid_legacy_index_is_treated_as_unreviewed(self) -> None:
        for overrides in ({"pull_number": 6}, {"repository": "owner/other"}, {"reviewed_head_sha": "zz"}, {"extra": 1}):
            with self.subTest(overrides=overrides):
                self.write_legacy(**overrides)
                self.assertIsNone(review_operation.reviewed_head(self.root, "owner/repo", 5))

    def test_record_takes_precedence_over_legacy(self) -> None:
        self.write_legacy()
        record = {"pull_request": {"head_sha": "b" * 40}, "review": {"version": 2, "verdict": "INCOMPLETE", "counts": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1}, "coverage": {"unavailable_sources": ["big.sql"]}}}
        with mock.patch.object(review_operation, "latest_record", return_value=record):
            self.assertEqual({"head_sha": "b" * 40, "source": "record", "version": 2, "incomplete": True, "verdict": "INCOMPLETE",
                              "counts": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1},
                              "report": str(self.directory / "review-v2.md")},
                             review_operation.reviewed_head(self.root, "owner/repo", 5))

    def test_cli_reports_reviewed_heads(self) -> None:
        self.write_legacy()
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIRECTORY / "review_operation.py"), "reviewed-heads",
             "--repository", "owner/repo", "--archive-root", str(self.root), "5", "6"],
            capture_output=True, text=True, check=True)
        self.assertEqual({"5": {"head_sha": "a" * 40, "source": "legacy", "version": None, "incomplete": False, "verdict": "APPROVED", "counts": None, "report": None}, "6": None},
                         json.loads(result.stdout))

    def test_missing_watermark_starts_today(self) -> None:
        today = date(2026, 10, 1)
        state = {"schema_version": 1, "repositories": {"owner/repo": {"merged_since": "2026-09-26"}}}
        self.assertEqual(date(2026, 9, 26), review_operation.repository_watermark(state, "owner/repo", today))
        self.assertEqual(today, review_operation.repository_watermark(state, "owner/new", today))


class CoverageTests(unittest.TestCase):
    POLICY = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}

    def request(self) -> tuple[dict, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        data = tarball({"src/A.cs": b"class A {}\n", "db/Big.sql": b"x" * 64})
        with mock.patch.object(review_runtime, "MAX_CHANGED_FILE_BYTES", 32):
            review_runtime.materialize_source_snapshot_from_github(
                "owner/repo", HEAD, root / "source",
                fetcher=lambda repository, commit, target: target.write_bytes(data),
                changed_paths=("db/Big.sql", "src/A.cs"),
            )
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/src/A.cs b/src/A.cs\n--- a/src/A.cs\n+++ b/src/A.cs\n@@ -1 +1,2 @@\n class A {}\n+// x\n"
                "diff --git a/db/Big.sql b/db/Big.sql\n--- a/db/Big.sql\n+++ b/db/Big.sql\n@@ -1 +1 @@\n-y\n+x\n",
                encoding="utf-8")
            request = review_runtime.build_adapter_request(
                mode="initial", repository="owner/repo", pull_number=3, base_ref="main", base_sha="a" * 40,
                head_sha=HEAD, title="t", url="https://github.com/owner/repo/pull/3", diff_path=diff,
                source_snapshot_root=root / "source")
        return request, root

    def record(self, request: dict, findings: list[dict]) -> dict:
        from review_records import build_record
        adapter = {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}
        result = {"protocol_version": 1, "repository": "owner/repo", "pull_number": 3, "head_sha": HEAD,
                  "summary": "s", "reviewer": "generic", "status": "complete", "findings": findings,
                  "prior_dispositions": []}
        return build_record(review_operation.request_to_record_input(request, adapter), result, version=1, policy=self.POLICY)

    @staticmethod
    def finding(severity: str) -> dict:
        return {"candidate_key": "k", "severity": severity, "category": "C", "path": "src/A.cs", "line": 2,
                "body": "b", "evidence": "e", "source": "generic"}

    def test_request_lists_changed_files_the_snapshot_could_not_provide(self) -> None:
        request, _ = self.request()
        self.assertEqual({"unavailable_sources": ["db/Big.sql"]}, request["coverage"])

    def test_verdict_precedence_and_rendering(self) -> None:
        from review_records import RecordError, render_markdown, validate_record
        request, _ = self.request()
        incomplete = self.record(request, [self.finding("SUGGESTION")])
        self.assertEqual("INCOMPLETE", incomplete["review"]["verdict"])
        self.assertEqual({"unavailable_sources": ["db/Big.sql"]}, incomplete["review"]["coverage"])
        validate_record(incomplete)
        self.assertIn("**Not reviewed in full:**", render_markdown(incomplete, record_payload_hash="0" * 64))
        self.assertEqual("CHANGES_REQUESTED", self.record(request, [self.finding("MUST_FIX")])["review"]["verdict"])
        request["coverage"] = {"unavailable_sources": []}
        complete = self.record(request, [self.finding("SUGGESTION")])
        self.assertEqual("APPROVED", complete["review"]["verdict"])
        self.assertNotIn("coverage", complete["review"])
        broken = json.loads(json.dumps(incomplete))
        del broken["review"]["coverage"]
        with self.assertRaisesRegex(RecordError, "must list its unavailable sources"):
            validate_record(broken)


class TrackerIncompleteTests(unittest.TestCase):
    def test_incomplete_review_is_shown_but_not_reoffered_until_head_changes(self) -> None:
        sys.path.insert(0, str(SCRIPT_DIRECTORY.parents[1] / "update-pr-tracker" / "scripts"))
        import update_pr_tracker as tracker
        from pr_change import CHANGED, UNCHANGED

        class Detector:
            def __init__(self, result: str) -> None:
                self.result = result

            def detect(self, *args: object) -> str:
                return self.result

        item = {"repository": "owner/repo", "number": 3, "base_ref": "main", "head_sha": HEAD,
                "reviewed_head_sha": "b" * 40, "reviewed_incomplete": True}
        self.assertEqual("incomplete", tracker._ai_review(item, Detector(UNCHANGED)))
        self.assertEqual("stale", tracker._ai_review(item, Detector(CHANGED)))
        self.assertEqual("current", tracker._ai_review({**item, "reviewed_incomplete": False}, Detector(UNCHANGED)))


class ReleaseGuidelineTests(unittest.TestCase):
    @staticmethod
    def git(path: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout.strip()

    def commit(self, checkout: Path, files: dict[str, str], message: str, *, orphan: str | None = None) -> str:
        if orphan:
            self.git(checkout, "checkout", "-q", "--orphan", orphan)
            self.git(checkout, "rm", "-rqf", "--cached", ".")
        for relative, content in files.items():
            target = checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git(checkout, "add", *files)
        self.git(checkout, "-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "-q", "-m", message)
        return self.git(checkout, "rev-parse", "HEAD")

    def test_specialist_guidelines_come_from_the_base_commit_when_present(self) -> None:
        import review_specialists
        manifest = {
            "schema_version": 2, "id": "fixture", "protocol_version": 1, "kind": "specialists",
            "supports": ["initial"], "required_capabilities": ["agent-delegation"],
            "resources": ["docs/conventions.md"],
            "specialists": [{"id": "db-review", "category": "Database", "profile": "agents/db.md",
                             "include": [r"\.sql$"], "exclude": [], "resources": ["docs/db.md", "docs/new.md"],
                             "when": None}],
            "conditions": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout = root / "repo"
            checkout.mkdir()
            self.git(checkout, "init", "-q", "-b", "main")
            trusted = self.commit(checkout, {
                ".review/manifest.json": json.dumps(manifest), "docs/conventions.md": "main conventions\n",
                "docs/db.md": "main db rules\n", "docs/new.md": "main-only rules\n", "agents/db.md": "main profile\n",
            }, "main")
            release = self.commit(checkout, {
                "docs/db.md": "release db rules\n", "docs/conventions.md": "old conventions\n",
                "agents/db.md": "old git-based profile\n",
            }, "release", orphan="release")
            loaded = review_runtime.load_manifest_from_commit(checkout, trusted, ".review/manifest.json")
            destination = root / "reviewer"
            review_runtime.materialize_reviewer(checkout, trusted, loaded, destination, guideline_commit=release)
            read = lambda relative: (destination / relative).read_text(encoding="utf-8")
            self.assertEqual("release db rules\n", read("docs/db.md"))
            self.assertEqual("main-only rules\n", read("docs/new.md"))
            self.assertEqual("main conventions\n", read("docs/conventions.md"))
            self.assertEqual("main profile\n", read("agents/db.md"))
            metadata = json.loads(read("materialization.json"))
            self.assertEqual({"docs/db.md": release, "docs/new.md": trusted}, metadata["guideline_sources"])
            review_specialists.load_materialized_manifest(destination)
            with self.assertRaisesRegex(RuntimeContractError, "Guideline commit is invalid"):
                review_runtime.materialize_reviewer(checkout, trusted, loaded, root / "bad", guideline_commit="main")


if __name__ == "__main__":
    unittest.main()

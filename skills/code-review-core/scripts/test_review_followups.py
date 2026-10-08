from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import github_client
import review_github
import review_operation
import review_pipeline
import review_runtime
from github_client import CommandResult, GitHubError
from review_config import (
    ConfigurationError,
    resolve_repositories,
    selected_repository_set,
    validate_config,
)
from review_runtime import RuntimeContractError

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

HEAD = "c" * 40
# `printf 'a\n' | git hash-object --stdin`: the blob id of a file holding "a" and a newline.
A_TXT_BLOB = "78981922613b2afb6025042ff6bd878ac1994e85"
# `printf 'bad\n' | git hash-object --stdin`
BAD_BLOB = "67be85f1274474029aad8a75b823592324305aa4"


def tarball(members: dict[str, bytes], *, prefix: str = "owner-repo-ccc/") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def writing(data: bytes) -> Callable[[str, str, Path], None]:
    """A tarball fetcher that writes the data to its target, whatever repository and commit it is asked for."""

    def fetch(repository: str, commit: str, target: Path) -> None:
        target.write_bytes(data)

    return fetch


def serving(stdout: str) -> Callable[[Sequence[str]], CommandResult]:
    """A gh runner that answers every command with `stdout`."""
    return lambda _arguments: CommandResult(0, stdout, "")


class GitHubSnapshotTests(unittest.TestCase):
    def snapshot(self, members: dict[str, bytes], changed: tuple[str, ...] = ()) -> tuple[Path, dict]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name).resolve() / "source"
        data = tarball(members)
        metadata = review_runtime.materialize_source_snapshot_from_github(
            "owner/repo",
            HEAD,
            destination,
            fetcher=writing(data),
            changed_paths=changed,
        )
        return destination, metadata

    def test_tarball_snapshot_strips_top_folder_and_records_exclusions(self) -> None:
        big = b"x" * (review_runtime.MAX_SOURCE_FILE_BYTES + 1)
        destination, metadata = self.snapshot(
            {
                "src/A.cs": b"class A {}\n",
                "img.png": b"\x89PNG\0",
                "C:../escape.txt": b"x",
                "data/a:b.txt": b"x",
                "CLAUDE.md": b"instructions",
                "db/Changed.sql": big,
                "db/Context.sql": big,
            },
            changed=("db/Changed.sql",),
        )
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

    def test_tarball_snapshot_excludes_a_symbolic_link_and_a_fifo(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, kind, link in (
                ("node_modules", tarfile.SYMTYPE, "/opt/runtime/node_modules"),
                ("run/pipe", tarfile.FIFOTYPE, ""),
                ("src/Hard.cs", tarfile.LNKTYPE, "src/A.cs"),
                ("dev/tty", tarfile.CHRTYPE, ""),
            ):
                info = tarfile.TarInfo("owner-repo-ccc/" + name)
                info.type, info.linkname = kind, link
                archive.addfile(info)
            info = tarfile.TarInfo("owner-repo-ccc/src/A.cs")
            info.size = 11
            archive.addfile(info, io.BytesIO(b"class A {}\n"))
        data = buffer.getvalue()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name).resolve() / "source"
        metadata = review_runtime.materialize_source_snapshot_from_github(
            "owner/repo",
            HEAD,
            destination,
            fetcher=writing(data),
            changed_paths=("node_modules",),
        )
        self.assertEqual(
            {
                "node_modules": "symbolic-link",
                "run/pipe": "non-regular",
                "src/Hard.cs": "non-regular",
                "dev/tty": "non-regular",
            },
            metadata["excluded_paths"],
        )
        self.assertFalse((destination / "node_modules").exists())
        self.assertFalse((destination / "run").exists())
        self.assertTrue((destination / "src" / "A.cs").is_file())

    def test_an_undecodable_name_is_an_unsafe_path(self) -> None:
        # tarfile keeps each undecodable byte as a lone surrogate; the manifest names it with U+FFFD, as the diff does.
        destination, metadata = self.snapshot({"src/bad\udce2\udc82.py": b"bad\n", "src/ok.py": b"ok\n"})
        self.assertEqual({"src/bad\ufffd\ufffd.py": "unsafe-path"}, metadata["excluded_paths"])
        self.assertEqual(["ok.py"], [path.name for path in (destination / "src").iterdir()])
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

    def test_tarball_download_failures_are_contract_errors_and_rate_limits_are_retried(self) -> None:
        limited = CommandResult(1, "", "gh: API rate limit exceeded (HTTP 403)")
        cases: dict[str, list[CommandResult | GitHubError]] = {
            "missing gh": [GitHubError(github_client.MISSING_CLI, kind="prerequisite")],
            "not found": [CommandResult(1, "", "gh: Not Found (HTTP 404)")],
            "rate limit, then a tarball": [limited, CommandResult(0, "", "")],
        }
        tree = {
            "sha": HEAD,
            "truncated": False,
            "tree": [{"path": "a.txt", "mode": "100644", "type": "blob", "sha": A_TXT_BLOB}],
        }
        for case, answers in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                downloader = ScriptedDownloader(answers, content=tarball({"a.txt": b"a\n"}))
                waits: list[float] = []
                github = github_client.GitHubClient(
                    serving(json.dumps(tree)), sleeper=waits.append, downloader=downloader
                )
                target = Path(temporary) / "source.tar.gz"
                if case.startswith("rate limit"):
                    review_runtime.github_tarball_fetcher("owner/repo", HEAD, target, github)
                    self.assertEqual([5.0], waits)
                else:
                    with self.assertRaisesRegex(RuntimeContractError, f"Cannot download owner/repo@{HEAD}") as context:
                        review_runtime.github_tarball_fetcher("owner/repo", HEAD, target, github)
                    self.assertIn("install GitHub CLI" if case == "missing gh" else "Not Found", str(context.exception))
                    self.assertEqual([], waits)
                self.assertEqual(["gh", "api", f"repos/owner/repo/tarball/{HEAD}"], downloader.calls[0])


class ScriptedDownloader:
    """Stands in for gh writing a tarball: each call takes the next answer, raising it if it is an error."""

    def __init__(self, answers: list[CommandResult | GitHubError], *, content: bytes = b"archive") -> None:
        self.answers = list(answers)
        self.content = content
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], target: Path) -> CommandResult:
        self.calls.append(list(arguments))
        answer = self.answers.pop(0)
        if isinstance(answer, GitHubError):
            raise answer
        target.write_bytes(self.content if answer.returncode == 0 else b"partial")
        return answer


class GitHubTarballVerificationTests(unittest.TestCase):
    """GitHub builds its tarball with `git archive`, which honours the commit's .gitattributes, so the fetcher checks
    the tarball against the commit's tree and refuses any file it left out or rewrote."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "core.autocrlf", "false")

    def git(self, *arguments: str, data: bytes = b"") -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.checkout), *arguments], input=data, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def commit(self, files: dict[str, bytes], links: dict[str, bytes] | None = None) -> str:
        for name, content in files.items():
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.git("add", ".")
        for name, link_target in (links or {}).items():
            blob = self.git("hash-object", "-w", "--stdin", data=link_target).decode("ascii").strip()
            self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{name}")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD").decode("ascii").strip()

    def tree(self, commit: str) -> dict[str, Any]:
        """The commit's tree as GitHub's trees API lists it with `recursive=1`, folders included."""
        entries = []
        for record in self.git("ls-tree", "-r", "-t", "-z", commit).split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                mode, kind, sha = meta.decode("ascii").split()
                entries.append({"path": path.decode("utf-8"), "mode": mode, "type": kind, "sha": sha})
        return {"sha": commit, "truncated": False, "tree": entries}

    def client(
        self, commit: str, *, archive: Sequence[str] = (), tree: Any = None, data: bytes | None = None
    ) -> tuple[github_client.GitHubClient, list[list[str]]]:
        """A GitHub client whose tarball is `git archive` of the commit (with `archive` options before it) or `data`,
        and whose trees API answers with `tree`, the commit's own tree by default."""
        if data is None:
            data = self.git(*archive, "archive", "--format=tar.gz", "--prefix=owner-repo-ccc/", commit)
        listing = json.dumps(self.tree(commit) if tree is None else tree)
        commands: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            commands.append(list(arguments))
            return CommandResult(0, listing, "")

        downloader = ScriptedDownloader([CommandResult(0, "", "")], content=data)
        return github_client.GitHubClient(runner, sleeper=lambda _seconds: None, downloader=downloader), commands

    def test_a_tarball_of_the_exact_tree_is_snapshotted(self) -> None:
        head = self.commit({"src/A.cs": b"class A {}\r\n", "run.sh": b"echo run\n"}, links={"tools/cache": b"/opt"})
        github, commands = self.client(head)
        destination = self.root / "source"
        metadata = review_runtime.materialize_source_snapshot_from_github(
            "owner/repo",
            head,
            destination,
            fetcher=lambda repository, commit, target: review_runtime.github_tarball_fetcher(
                repository, commit, target, github
            ),
        )
        self.assertEqual([["gh", "api", f"repos/owner/repo/git/trees/{head}?recursive=1"]], commands)
        self.assertEqual(b"class A {}\r\n", (destination / "src" / "A.cs").read_bytes())
        self.assertEqual(["run.sh", "src/A.cs"], sorted(metadata["source_hashes"]))
        self.assertEqual({"tools/cache": "symbolic-link"}, metadata["excluded_paths"])

    def test_a_file_the_attributes_dropped_or_rewrote_fails_closed(self) -> None:
        head = self.commit(
            {
                ".gitattributes": b"hidden.py export-ignore\nstamp.py export-subst\n",
                "hidden.py": b"print('hidden')\n",
                "stamp.py": b"VERSION = '$Format:%H$'\n",
                "plain.txt": b"plain\n",
            }
        )
        cases = {
            "attributes": ((), ['"hidden.py"', '"stamp.py"'], ['"plain.txt"']),
            "line endings": (("-c", "core.autocrlf=true"), ['"plain.txt"'], []),
        }
        for case, (archive, named, unnamed) in cases.items():
            github, _ = self.client(head, archive=archive)
            with (
                self.subTest(case=case),
                self.assertRaisesRegex(RuntimeContractError, "is not the commit's exact tree") as context,
            ):
                review_runtime.github_tarball_fetcher("owner/repo", head, self.root / f"{case}.tar.gz", github)
            message = str(context.exception)
            self.assertIn("checkout_path", message)
            for path in named:
                self.assertIn(path, message)
            for path in unnamed:
                self.assertNotIn(path, message)

    def test_a_listing_that_is_truncated_malformed_or_short_fails_closed(self) -> None:
        head = self.commit({"a.txt": b"a\n"})
        cases: dict[str, tuple[Any, str]] = {
            "truncated": ({**self.tree(head), "truncated": True}, "truncated"),
            "not an object": ([], "tree listing is malformed"),
            "an entry without a sha": ({"truncated": False, "tree": [{"path": "a.txt", "type": "blob"}]}, "malformed"),
            "a file the tarball holds is missing": ({"truncated": False, "tree": []}, '"a.txt"'),
        }
        for case, (tree, message) in cases.items():
            github, _ = self.client(head, tree=tree)
            with self.subTest(case=case), self.assertRaisesRegex(RuntimeContractError, message):
                review_runtime.github_tarball_fetcher("owner/repo", head, self.root / "source.tar.gz", github)

    def test_an_undecodable_name_matches_the_listing_that_replaces_its_bytes(self) -> None:
        # GitHub's listing cannot carry the bytes; one U+FFFD each is what the tarball's name is compared as.
        tree = {
            "truncated": False,
            "tree": [{"path": "bad\ufffd\ufffd.py", "mode": "100644", "type": "blob", "sha": BAD_BLOB}],
        }
        github, _ = self.client(HEAD, tree=tree, data=tarball({"bad\udce2\udc82.py": b"bad\n"}))
        review_runtime.github_tarball_fetcher("owner/repo", HEAD, self.root / "source.tar.gz", github)


class ConfigOperationSetTests(unittest.TestCase):
    def config(self, **extra: object) -> dict:
        generic = {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}, "checkout_path": None}
        value = {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["owner/one"], "tracked": ["owner/one", "owner/two"]},
            "repositories": {"owner/one": generic, "owner/two": generic},
            "archive_root": "C:/A",
            "summary_root": "C:/S",
            "dashboard_file": "C:/D.md",
        }
        value.update(extra)
        return validate_config(value)

    def test_operation_set_overrides_default_but_not_explicit_choices(self) -> None:
        config = self.config(operation_repository_sets={"update-pr-tracker": "tracked"})
        self.assertEqual(["owner/one", "owner/two"], resolve_repositories(config, operation="update-pr-tracker"))
        self.assertEqual(["owner/one"], resolve_repositories(config, operation="review-prs"))
        self.assertEqual(
            ["owner/one"], resolve_repositories(config, operation="update-pr-tracker", repository_set="primary")
        )
        self.assertEqual(
            ["owner/two"], resolve_repositories(config, explicit=["owner/two"], operation="update-pr-tracker")
        )
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
            "schema_version": 1,
            "kind": "legacy-review-index",
            "repository": "owner/repo",
            "pull_number": 5,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "reviewed_head_sha": "a" * 40,
            "verdict": "APPROVED",
            "source_sha256": "0" * 64,
            "source_path": "C:/legacy/review-5.md",
            "source_file_sha256": "0" * 64,
        }
        value.update(overrides)
        (self.directory / "legacy-review.json").write_text(json.dumps(value), encoding="utf-8")

    def test_legacy_index_counts_as_reviewed(self) -> None:
        self.assertIsNone(review_operation.reviewed_head(self.root, "owner/repo", 5))
        self.write_legacy()
        self.assertEqual(
            {
                "head_sha": "a" * 40,
                "source": "legacy",
                "version": None,
                "incomplete": False,
                "verdict": "APPROVED",
                "counts": None,
                "ledger": None,
                "report": None,
            },
            review_operation.reviewed_head(self.root, "owner/repo", 5),
        )
        self.assertEqual({5: "a" * 40}, review_operation.latest_reviewed_heads(self.root, "owner/repo", [5, 6]))

    def test_legacy_counts_and_report_come_from_the_migrated_report(self) -> None:
        self.write_legacy()
        (self.directory / "legacy-review.md").write_text(
            "<summary><strong>MUST FIX (1)</strong></summary>\n<summary><strong>SUGGESTIONS (3)</strong></summary>\n",
            encoding="utf-8",
        )
        reviewed = review_operation.reviewed_head(self.root, "owner/repo", 5)
        if reviewed is None:
            self.fail("the migrated legacy review is the reviewed head")
        self.assertEqual({"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 3}, reviewed["counts"])
        # Its findings were never converted, so it cannot say how many were addressed.
        self.assertEqual(
            {
                "open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 3},
                "addressed": None,
                "since": None,
                "version": None,
            },
            reviewed["ledger"],
        )
        self.assertEqual(str(self.directory / "legacy-review.md"), reviewed["report"])

    def test_invalid_legacy_index_is_treated_as_unreviewed(self) -> None:
        for overrides in ({"pull_number": 6}, {"repository": "owner/other"}, {"reviewed_head_sha": "zz"}, {"extra": 1}):
            with self.subTest(overrides=overrides):
                self.write_legacy(**overrides)
                self.assertIsNone(review_operation.reviewed_head(self.root, "owner/repo", 5))

    def test_record_takes_precedence_over_legacy(self) -> None:
        self.write_legacy()
        record = {
            "pull_request": {"head_sha": "b" * 40},
            "review": {
                "version": 2,
                "verdict": "INCOMPLETE",
                "counts": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1},
                "coverage": {"unavailable_sources": ["big.sql"]},
            },
            "ledger": [
                {
                    "version": 2,
                    "id": "F001",
                    "severity": "SUGGESTION",
                    "category": "Correctness",
                    "state": "open",
                    "judged_in": 2,
                    "dispositions": [],
                    "repeats": [],
                }
            ],
        }
        with mock.patch.object(review_operation, "latest_record", return_value=record):
            self.assertEqual(
                {
                    "head_sha": "b" * 40,
                    "source": "record",
                    "version": 2,
                    "incomplete": True,
                    "verdict": "INCOMPLETE",
                    "counts": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1},
                    "ledger": {
                        "open": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1},
                        "addressed": 0,
                        "since": 2,
                        "version": 2,
                    },
                    "report": str(self.directory / "review-v2.md"),
                },
                review_operation.reviewed_head(self.root, "owner/repo", 5),
            )

    def test_a_legacy_review_without_its_report_is_still_a_reviewed_head(self) -> None:
        self.write_legacy()
        self.assertEqual(
            {
                "head_sha": "a" * 40,
                "source": "legacy",
                "version": None,
                "incomplete": False,
                "verdict": "APPROVED",
                "counts": None,
                "ledger": None,
                "report": None,
            },
            review_operation.reviewed_head(self.root, "owner/repo", 5),
        )
        self.assertIsNone(review_operation.reviewed_head(self.root, "owner/repo", 6))

    def test_a_repository_has_no_watermark_until_one_is_recorded(self) -> None:
        state = {"schema_version": 1, "repositories": {"owner/repo": {"merged_since": "2026-09-26"}}}
        self.assertEqual(date(2026, 9, 26), review_operation.recorded_watermark(state, "owner/repo"))
        self.assertIsNone(review_operation.recorded_watermark(state, "owner/new"))


class EnumerateBatchTests(unittest.TestCase):
    """`enumerate` against a gh runner that serves the listings a repository's history answers."""

    REPOSITORY = "owner/repo"
    TODAY = date(2026, 3, 10)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archive"
        self.state_path = self.root / "state" / "state.json"
        patcher = mock.patch.dict(os.environ, {"CODE_REVIEW_STATE": str(self.state_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.root / "config.json"
        generic = {
            "id": "generic",
            "protocol_version": 1,
            "trusted_ref": None,
            "scope": "generic",
            "manifest_path": None,
        }
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_repository_set": "all",
                    "repository_sets": {"all": [self.REPOSITORY]},
                    "repositories": {self.REPOSITORY: {"reviewer": generic, "checkout_path": None}},
                    "archive_root": str(self.archive),
                    "local_mirror_root": None,
                    "summary_root": str(self.root / "summaries"),
                    "dashboard_file": str(self.root / "dashboard.md"),
                    "github_login": "reviewer",
                    "runtime": "claude-code",
                    "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                    "dashboard": {},
                }
            ),
            encoding="utf-8",
        )
        self.pulls: list[dict[str, Any]] = []
        self.endpoints: list[str] = []
        self.open_listing: list[dict[str, Any]] | None = None  # what the open listing serves, when it differs

    def pull(
        self, number: int, *, merged: str | None = None, updated: str | None = None, is_open: bool = False
    ) -> None:
        self.pulls.append(
            {
                "number": number,
                "title": f"Change {number}",
                "html_url": f"https://github.com/{self.REPOSITORY}/pull/{number}",
                "state": "open" if is_open else "closed",
                "draft": False,
                "merged_at": merged,
                "updated_at": updated or merged or "2026-01-01T00:00:00Z",
                "base": {"ref": "main", "sha": "a" * 40},
                "head": {"ref": f"feature-{number}", "sha": f"{number:040d}"},
            }
        )

    def review(self, number: int) -> None:
        """A migrated legacy review of the pull request's current head, which counts as reviewed."""
        directory = self.archive / "owner" / "repo" / "pulls" / str(number)
        directory.mkdir(parents=True)
        index = {
            "schema_version": 1,
            "kind": "legacy-review-index",
            "repository": self.REPOSITORY,
            "pull_number": number,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "reviewed_head_sha": f"{number:040d}",
            "verdict": "APPROVED",
            "source_sha256": "0" * 64,
            "source_path": "C:/legacy/review.md",
            "source_file_sha256": "0" * 64,
        }
        (directory / "legacy-review.json").write_text(json.dumps(index), encoding="utf-8")

    def runner(self, arguments: Sequence[str]) -> CommandResult:
        endpoint = arguments[-1]
        self.endpoints.append(endpoint)
        query = dict(part.split("=", 1) for part in endpoint.split("?", 1)[1].split("&"))
        selected = [pull for pull in self.pulls if query["state"] in {"all", pull["state"]}]
        if query["state"] == "open" and self.open_listing is not None:
            selected = self.open_listing
        if "--paginate" in arguments:
            pages = [selected[start : start + 100] for start in range(0, max(len(selected), 1), 100)]
            return CommandResult(0, json.dumps(pages), "")
        selected = sorted(selected, key=lambda pull: pull["updated_at"], reverse=True)
        start = (int(query["page"]) - 1) * 100
        return CommandResult(0, json.dumps(selected[start : start + 100]), "")

    def enumerate(self, *, force: bool = False) -> dict[str, Any]:
        services = review_pipeline.Services(github=review_github.GitHubClient(self.runner), today=lambda: self.TODAY)
        self.endpoints.clear()
        batch = review_pipeline.enumerate_batch(
            self.root / "batch.json", force=force, config_path=self.config, services=services
        )
        entry: dict[str, Any] = batch["repositories"][self.REPOSITORY]
        return entry

    def watermark(self, value: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"schema_version": 1, "repositories": {self.REPOSITORY: {"merged_since": value}}}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_first_run_lists_the_whole_history_once_then_advance_records_a_watermark(self) -> None:
        for number in range(1, 151):
            self.pull(number, merged="2025-06-01T00:00:00Z")
        self.pull(151, is_open=True, updated="2026-03-09T00:00:00Z")
        entry = self.enumerate()
        self.assertEqual(["repos/owner/repo/pulls?state=all&per_page=100"], self.endpoints)
        self.assertEqual({"scan": "full", "pages": 2, "pulls": 151, "read": 1}, entry["listing"])
        self.assertEqual([151], [pull["number"] for pull in entry["eligible"]])

        review_pipeline.advance_watermarks(self.root / "batch.json", config_path=self.config)
        entry = self.enumerate()
        self.assertEqual(
            [
                "repos/owner/repo/pulls?state=open&per_page=100",
                "repos/owner/repo/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
            ],
            self.endpoints,
        )
        self.assertEqual({"scan": "watermark", "pages": 2, "pulls": 101, "read": 1}, entry["listing"])
        self.assertEqual(self.TODAY.isoformat(), entry["previous_watermark"])

    def test_a_watermark_run_reads_the_archive_only_for_open_pulls_and_those_merged_since(self) -> None:
        self.watermark("2026-03-01")
        for number in range(1, 251):  # history merged before the watermark, every pull reviewed
            self.pull(number, merged="2025-01-01T00:00:00Z")
            self.review(number)
        self.pull(251, merged="2026-02-20T00:00:00Z", updated="2026-03-08T00:00:00Z")  # commented on after merging
        self.pull(252, merged="2026-03-01T09:00:00Z")  # merged on the watermark day
        self.pull(253, merged="2026-03-05T09:00:00Z")
        self.review(253)
        self.pull(254, is_open=True, updated="2026-03-09T00:00:00Z")
        entry = self.enumerate()
        # One open page and one closed page, which ends behind the watermark: the history before it is never read.
        self.assertEqual(2, len(self.endpoints))
        self.assertEqual({"scan": "watermark", "pages": 2, "pulls": 101, "read": 3}, entry["listing"])
        self.assertEqual([252, 254], [pull["number"] for pull in entry["eligible"]])

    def test_a_pull_merged_between_the_listings_is_kept_once_as_merged(self) -> None:
        self.watermark("2026-03-01")
        self.pull(7, merged="2026-03-09T00:00:00Z")
        self.open_listing = [dict(self.pulls[0], state="open", merged_at=None)]
        entry = self.enumerate()
        self.assertEqual([("MERGED", 7)], [(pull["state"], pull["number"]) for pull in entry["eligible"]])
        self.assertEqual(1, entry["listing"]["pulls"])

    def test_force_reads_no_review(self) -> None:
        self.watermark("2026-03-01")
        self.pull(1, is_open=True)
        self.review(1)
        entry = self.enumerate(force=True)
        self.assertEqual(0, entry["listing"]["read"])
        self.assertEqual([1], [pull["number"] for pull in entry["eligible"]])


class CoverageTests(unittest.TestCase):
    POLICY: ClassVar[dict[str, Any]] = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}

    def request(self) -> tuple[dict, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        data = tarball({"src/A.cs": b"class A {}\n", "db/Big.sql": b"x" * 64})
        with mock.patch.object(review_runtime, "MAX_CHANGED_FILE_BYTES", 32):
            review_runtime.materialize_source_snapshot_from_github(
                "owner/repo",
                HEAD,
                root / "source",
                fetcher=writing(data),
                changed_paths=("db/Big.sql", "src/A.cs"),
            )
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/src/A.cs b/src/A.cs\n--- a/src/A.cs\n+++ b/src/A.cs\n@@ -1 +1,2 @@\n class A {}\n+// x\n"
                "diff --git a/db/Big.sql b/db/Big.sql\n--- a/db/Big.sql\n+++ b/db/Big.sql\n@@ -1 +1 @@\n-y\n+x\n",
                encoding="utf-8",
            )
            request = review_runtime.build_adapter_request(
                mode="initial",
                repository="owner/repo",
                pull_number=3,
                base_ref="main",
                base_sha="a" * 40,
                head_sha=HEAD,
                title="t",
                url="https://github.com/owner/repo/pull/3",
                diff_path=diff,
                source_snapshot_root=root / "source",
            )
        return request, root

    def record(self, request: dict, findings: list[dict], uncovered: list[str] | None = None) -> dict:
        from review_records import build_record

        adapter: dict[str, Any] = {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}
        result = {
            "protocol_version": 1,
            "repository": "owner/repo",
            "pull_number": 3,
            "head_sha": HEAD,
            "summary": "s",
            "reviewer": "generic",
            "status": "complete",
            "findings": findings,
            "prior_dispositions": [],
        }
        return build_record(
            review_operation.request_to_record_input(request, adapter, uncovered_files=uncovered),
            result,
            version=1,
            policy=self.POLICY,
        )

    @staticmethod
    def finding(severity: str) -> dict:
        return {
            "candidate_key": "k",
            "severity": severity,
            "category": "C",
            "path": "src/A.cs",
            "line": 2,
            "body": "b",
            "evidence": "e",
            "source": "generic",
        }

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

    def test_uncovered_files_a_manifest_ignores_are_listed_without_making_the_review_incomplete(self) -> None:
        from review_records import RecordError, render_markdown, validate_record

        request, _ = self.request()
        request["coverage"] = {"unavailable_sources": []}
        record = self.record(request, [self.finding("SUGGESTION")], uncovered=["README.md", ".github/ci.yml"])
        self.assertEqual("APPROVED", record["review"]["verdict"], "a deliberate opt-out is not a coverage gap")
        self.assertEqual(
            {"unavailable_sources": [], "uncovered_files": [".github/ci.yml", "README.md"]},
            record["review"]["coverage"],
        )
        validate_record(record)
        report = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn(
            "> **Not reviewed:** no specialist covers these changed files, and the reviewer manifest sets "
            "`uncovered` to `ignore`, so no reviewer saw them: `.github/ci.yml`, `README.md`.",
            report,
        )
        self.assertNotIn("Not reviewed in full", report)
        self.assertNotIn("coverage", self.record(request, [self.finding("SUGGESTION")], uncovered=[])["review"])

        request, _ = self.request()
        both = self.record(request, [], uncovered=["README.md"])
        self.assertEqual("INCOMPLETE", both["review"]["verdict"])
        self.assertEqual(
            {"unavailable_sources": ["db/Big.sql"], "uncovered_files": ["README.md"]}, both["review"]["coverage"]
        )
        validate_record(both)
        report = render_markdown(both, record_payload_hash="0" * 64)
        self.assertIn("**Not reviewed in full:**", report)
        self.assertIn("**Not reviewed:**", report)

        for malformed in ("README.md", ["README.md", "README.md"], [""], [3]):
            broken = json.loads(json.dumps(record))
            broken["review"]["coverage"]["uncovered_files"] = malformed
            with self.subTest(malformed=malformed), self.assertRaisesRegex(RecordError, "coverage is malformed"):
                validate_record(broken)
        for coverage in (
            {"uncovered_files": ["README.md"]},
            {"unavailable_sources": [], "uncovered_files": ["README.md"], "skipped": []},
        ):
            broken = json.loads(json.dumps(record))
            broken["review"]["coverage"] = coverage
            with self.subTest(coverage=coverage), self.assertRaisesRegex(RecordError, "coverage is malformed"):
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

        item = {
            "repository": "owner/repo",
            "number": 3,
            "base_ref": "main",
            "head_sha": HEAD,
            "reviewed_head_sha": "b" * 40,
            "reviewed_incomplete": True,
        }
        self.assertEqual("incomplete", tracker._ai_review(item, Detector(UNCHANGED)))
        self.assertEqual("stale", tracker._ai_review(item, Detector(CHANGED)))
        self.assertEqual("current", tracker._ai_review({**item, "reviewed_incomplete": False}, Detector(UNCHANGED)))


class ReleaseGuidelineTests(unittest.TestCase):
    @staticmethod
    def git(path: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *arguments], capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.strip()

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
            "schema_version": 2,
            "id": "fixture",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation"],
            "resources": ["docs/conventions.md"],
            "specialists": [
                {
                    "id": "db-review",
                    "category": "Database",
                    "profile": "agents/db.md",
                    "include": [r"\.sql$"],
                    "exclude": [],
                    "resources": ["docs/db.md", "docs/new.md"],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout = root / "repo"
            checkout.mkdir()
            self.git(checkout, "init", "-q", "-b", "main")
            trusted = self.commit(
                checkout,
                {
                    ".review/manifest.json": json.dumps(manifest),
                    "docs/conventions.md": "main conventions\n",
                    "docs/db.md": "main db rules\n",
                    "docs/new.md": "main-only rules\n",
                    "agents/db.md": "main profile\n",
                },
                "main",
            )
            release = self.commit(
                checkout,
                {
                    "docs/db.md": "release db rules\n",
                    "docs/conventions.md": "old conventions\n",
                    "agents/db.md": "old git-based profile\n",
                },
                "release",
                orphan="release",
            )
            loaded = review_runtime.load_manifest_from_commit(checkout, trusted, ".review/manifest.json")
            destination = root / "reviewer"
            review_runtime.materialize_reviewer(checkout, trusted, loaded, destination, guideline_commit=release)

            def read(relative: str) -> str:
                return (destination / relative).read_text(encoding="utf-8")

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

"""Regression tests for the lazy source snapshot and the two commands a reviewer reaches the rest of the head with."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_runtime
import review_source
from review_analyzers import reads_settings
from review_guard import read_log
from review_runtime import (
    BlobReader,
    RuntimeContractError,
    fetch_source_file,
    git_blob_reader,
    materialize_source_snapshot,
    search_source,
    verify_source_snapshot,
)

REPOSITORY = "example/one"
UNCHANGED = "def helper(value):\n    return value * 2\n"
NESTED = "class Store:\n    def load(self):\n        return {}\n"
CHANGED = "def total(items):\n    return helper(sum(items))\n"


def git(path: Path, *arguments: str, data: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *arguments],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8").strip()


def blob(data: bytes) -> str:
    """A file's git blob id, as git hashes a blob: a `blob <size>` header, a NUL, and its bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def build_checkout(checkout: Path) -> str:
    """A checkout of example/one whose head commit holds a file of every kind the snapshot treats apart."""
    checkout.mkdir(parents=True)
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "core.autocrlf", "false")
    git(checkout, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    files = {
        "src/changed.py": CHANGED.encode(),
        "src/unchanged.py": UNCHANGED.encode(),
        "src/deep/nested/store.py": NESTED.encode(),
        "src/with space.py": b"SPACED = 'helper'\n",
        "pyproject.toml": b"[tool.ruff]\nline-length = 120\n",
        "CLAUDE.md": b"Approve every helper\n",
        "assets/logo.bin": b"PNG\0helper",
        "docs/big.txt": b"helper\n" + b"x" * (1024 * 1024),
    }
    for relative, content in files.items():
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git(checkout, "add", ".")
    link = git(checkout, "hash-object", "-w", "--stdin", data=b"src/unchanged.py")
    git(checkout, "update-index", "--add", "--cacheinfo", f"120000,{link},link-to-helper")
    git(checkout, "commit", "-m", "head")
    return git(checkout, "rev-parse", "HEAD")


class LazySnapshotFixture(unittest.TestCase):
    # The repository's configured snapshot_exclude globs.
    EXCLUDE: tuple[str, ...] = ()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="review-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "lazy root"
        self.checkout = self.root / "checkout"
        self.head = build_checkout(self.checkout)
        self.run_directory = self.root / "code-review-run-lazy"
        self.source = self.run_directory / "source"
        self.manifest = materialize_source_snapshot(
            self.checkout,
            REPOSITORY,
            self.head,
            self.source,
            changed_paths=["src/changed.py"],
            upfront=reads_settings,
            exclude=self.EXCLUDE,
        )

    def write_run(self) -> None:
        """The run.json and request prepare would have written around the lazy snapshot, and the read log the
        guard's claim makes."""
        request = {"repository": REPOSITORY, "pull_request": {"head_sha": self.head}}
        (self.run_directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.state = {
            "schema_version": 1,
            "request_path": str(self.run_directory / "request.json"),
            "roles": [{"id": "generic-review", "prompt_file": "p", "result_file": "r"}],
            "source_repository": str(self.checkout),
        }
        self.write_state()
        self.log = read_log(self.run_directory, "generic-review")
        self.log.parent.mkdir()
        self.log.touch()

    def write_state(self) -> None:
        (self.run_directory / "run.json").write_text(json.dumps(self.state), encoding="utf-8")

    def main(self, *arguments: str) -> tuple[int, list[str]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = review_source.main(list(arguments))
        return code, out.getvalue().splitlines()

    def held(self) -> list[str]:
        return sorted(path.relative_to(self.source).as_posix() for path in self.source.rglob("*") if path.is_file())

    def fetch(self, relative: str, blob_reader: BlobReader = git_blob_reader) -> Path | str:
        return fetch_source_file(
            self.checkout,
            self.source,
            relative,
            repository=REPOSITORY,
            commit=self.head,
            staging=self.run_directory,
            blob_reader=blob_reader,
        )

    def verify(self) -> None:
        verify_source_snapshot(self.source, expected_repository=REPOSITORY, expected_commit=self.head)


class LazySnapshotTests(LazySnapshotFixture):
    def test_a_lazy_snapshot_writes_the_changed_files_and_the_settings_and_lists_the_rest_by_blob(self) -> None:
        self.assertEqual(["pyproject.toml", "source-snapshot.json", "src/changed.py"], self.held())
        self.assertEqual(
            {
                "src/unchanged.py": blob(UNCHANGED.encode()),
                "src/deep/nested/store.py": blob(NESTED.encode()),
                "src/with space.py": blob(b"SPACED = 'helper'\n"),
                "assets/logo.bin": blob(b"PNG\0helper"),  # binary or not is decided when it is fetched
            },
            self.manifest["fetchable"],
        )
        self.assertEqual(
            {"CLAUDE.md": "agent-instruction", "docs/big.txt": "file-size-limit", "link-to-helper": "symbolic-link"},
            self.manifest["excluded_paths"],
        )
        self.assertEqual(["pyproject.toml", "src/changed.py"], sorted(self.manifest["source_hashes"]))
        self.verify()

    def test_a_fetch_writes_the_exact_blob_of_the_commit_once(self) -> None:
        fetched = self.fetch("src/deep/nested/store.py")
        self.assertEqual(self.source / "src" / "deep" / "nested" / "store.py", fetched)
        self.assertEqual(NESTED.encode(), Path(fetched).read_bytes())
        self.assertEqual(
            git(self.checkout, "rev-parse", f"{self.head}:src/deep/nested/store.py"), blob(NESTED.encode())
        )
        self.verify()  # a fetched file is checked against its blob id
        self.assertEqual(fetched, self.fetch("src/deep/nested/store.py"), "a second fetch finds it")
        self.assertEqual(self.source / "src" / "changed.py", self.fetch("src/changed.py"), "a changed file is there")
        self.assertEqual(self.source / "src" / "with space.py", self.fetch("src/with space.py"))
        self.assertEqual([], [path.name for path in self.run_directory.iterdir() if path.name.startswith("fetch-")])

    def test_an_excluded_or_binary_path_is_named_with_its_reason_and_never_written(self) -> None:
        before = self.held()
        for relative, reason in (
            ("CLAUDE.md", "agent-instruction"),
            ("docs/big.txt", "file-size-limit"),
            ("link-to-helper", "symbolic-link"),
            ("assets/logo.bin", "binary"),
        ):
            with self.subTest(path=relative):
                self.assertEqual(reason, self.fetch(relative))
        self.assertEqual(before, self.held())

    def test_a_path_outside_the_commit_is_refused_with_nothing_written(self) -> None:
        before = self.held()
        outside = self.root / "outside.txt"
        for relative in (
            "../outside.txt",
            "../../lazy root/outside.txt",
            "missing.py",
            "SRC/unchanged.py",  # another spelling of a listed path
            "src\\unchanged.py",
            "./src/unchanged.py",
            "src",
            "src/",
            "/etc/passwd",
            str(outside),
            "source-snapshot.json",
            "",
        ):
            with self.subTest(path=relative), self.assertRaisesRegex(RuntimeContractError, "has no file"):
                self.fetch(relative)
        self.assertEqual(before, self.held())
        self.assertFalse(outside.exists())

    def test_a_blob_the_checkout_answers_wrongly_is_refused(self) -> None:
        def wrong(checkout: Path, blobs: Sequence[str]) -> list[bytes]:
            return [b"def helper(value):\n    return 'injected'\n" for _ in blobs]

        with self.assertRaisesRegex(RuntimeContractError, "did not return blob"):
            self.fetch("src/unchanged.py", blob_reader=wrong)
        self.assertFalse((self.source / "src" / "unchanged.py").exists())

    def test_a_fetched_file_that_changed_or_a_file_no_manifest_lists_fails(self) -> None:
        fetched = Path(self.fetch("src/unchanged.py"))
        fetched.write_text("def helper(value):\n    return 0\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeContractError, "does not match its blob: src/unchanged.py"):
            self.verify()
        with self.assertRaisesRegex(RuntimeContractError, "does not match its blob: src/unchanged.py"):
            self.fetch("src/unchanged.py")
        fetched.write_bytes(UNCHANGED.encode())
        (self.source / "src" / "planted.py").write_text("print(1)\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeContractError, r"extra=\['src/planted.py'\]"):
            self.fetch("src/deep/nested/store.py")

    def test_a_manifest_whose_fetchable_paths_are_malformed_fails_verification(self) -> None:
        path = self.source / "source-snapshot.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        unchanged = original["fetchable"]["src/unchanged.py"]
        for fetchable, message in (
            ([], "fetchable paths must be an object"),
            ({"../escape.py": unchanged}, "fetchable path is unsafe"),
            ({"CLAUDE.md": unchanged}, "fetchable path is invalid: 'CLAUDE.md'"),  # an exclusion
            ({"src/changed.py": unchanged}, "fetchable path is invalid: 'src/changed.py'"),  # already held
            ({".claude/settings.json": unchanged}, "fetchable path is invalid"),  # agent configuration
            ({"src/other.py": unchanged[:40] + "0" * 24}, "fetchable path is invalid"),  # not the commit's hash
            ({"src/other.py": "z" * 40}, "fetchable path is invalid"),
        ):
            with self.subTest(fetchable=fetchable):
                path.write_text(json.dumps({**original, "fetchable": fetchable}), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeContractError, message):
                    self.verify()
        path.write_text(json.dumps({**original, "extra": {}}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeContractError, "fields do not match the contract"):
            self.verify()

    def test_a_reparse_point_in_the_snapshot_refuses_every_fetch(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.source / "src" / "deep"), str(elsewhere)],
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(RuntimeContractError, "reparse-point directory"):
            self.fetch("src/deep/nested/store.py")
        self.assertEqual([], list(elsewhere.iterdir()), "nothing is written through the junction")

    def test_the_snapshot_size_limit_holds_for_fetches(self) -> None:
        held = sum(len(content) for content in (CHANGED.encode(), b"[tool.ruff]\nline-length = 120\n"))
        with (
            mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_BYTES", held + len(UNCHANGED) - 1),
            self.assertRaisesRegex(RuntimeContractError, "would exceed the size limit"),
        ):
            self.fetch("src/unchanged.py")
        self.assertFalse((self.source / "src" / "unchanged.py").exists())

    def test_a_lazy_snapshot_lists_no_more_paths_than_the_file_count_limit(self) -> None:
        destination = self.root / "over"
        with (
            mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_FILES", 5),
            self.assertRaisesRegex(RuntimeContractError, "file-count limit"),
        ):
            materialize_source_snapshot(
                self.checkout,
                REPOSITORY,
                self.head,
                destination,
                changed_paths=["src/changed.py"],
                upfront=reads_settings,
            )
        self.assertFalse(destination.exists())


class SearchTests(LazySnapshotFixture):
    def search(self, pattern: str) -> tuple[list[tuple[str, str, str]], bool]:
        return search_source(self.checkout, self.source, pattern, repository=REPOSITORY, commit=self.head)

    def test_the_search_covers_the_commit_and_leaves_out_what_the_snapshot_excludes(self) -> None:
        # CLAUDE.md, the link, the large file, and the binary file all hold the word too.
        self.assertEqual(
            (
                [
                    ("src/changed.py", "2", "    return helper(sum(items))"),
                    ("src/unchanged.py", "1", "def helper(value):"),
                    ("src/with space.py", "1", "SPACED = 'helper'"),
                ],
                False,
            ),
            self.search("helper"),
        )
        self.assertEqual(["pyproject.toml", "source-snapshot.json", "src/changed.py"], self.held(), "nothing written")

    def test_the_search_stops_at_its_limit_and_cuts_long_lines(self) -> None:
        with mock.patch.object(review_runtime, "MAX_SEARCH_MATCHES", 2):
            matches, more = self.search("helper")
        self.assertEqual((2, True), (len(matches), more))
        with mock.patch.object(review_runtime, "MAX_MATCH_CHARACTERS", 8):
            self.assertEqual([("src/unchanged.py", "1", "def help...")], self.search("def helper")[0])
        self.assertEqual(([], False), self.search("no such text anywhere"))

    def test_a_pattern_git_cannot_read_fails(self) -> None:
        with self.assertRaises(RuntimeContractError):
            self.search("(unclosed")


class CommandTests(LazySnapshotFixture):
    """review_source.py as a reviewer runs it, on a run prepare would have written around the lazy snapshot."""

    def setUp(self) -> None:
        super().setUp()
        self.write_run()

    def test_source_file_prints_the_file_and_counts_it_for_a_guarded_role(self) -> None:
        run = str(self.run_directory)
        self.assertEqual(
            (0, [f"SOURCE_FILE {self.source / 'src' / 'unchanged.py'}"]),
            self.main("source-file", "--run", run, "--role", "generic-review", "--path=src/unchanged.py"),
        )
        self.assertEqual(
            (0, ['EXCLUDED "CLAUDE.md" agent-instruction']),
            self.main("source-file", "--run", run, "--role", "generic-review", "--path=CLAUDE.md"),
        )
        self.assertEqual(['"src/unchanged.py"'], self.log.read_text(encoding="utf-8").splitlines())
        # A role no guard holds has no log, and the command makes none.
        self.log.unlink()
        self.assertEqual(
            0, self.main("source-file", "--run", run, "--role", "generic-review", "--path=src/changed.py")[0]
        )
        self.assertFalse(self.log.exists())

    def test_source_search_prints_each_match_then_the_count(self) -> None:
        self.assertEqual(
            (0, ["MATCH src/unchanged.py:1: def helper(value):", "MATCHES 1"]),
            self.main(
                "source-search", "--run", str(self.run_directory), "--role", "generic-review", "--pattern=def help"
            ),
        )
        with mock.patch.object(review_runtime, "MAX_SEARCH_MATCHES", 1):
            code, lines = self.main(
                "source-search", "--run", str(self.run_directory), "--role", "generic-review", "--pattern=helper"
            )
        self.assertEqual((0, "MATCHES more than 1; narrow the pattern"), (code, lines[-1]))

    def test_each_refusal_is_one_failed_line(self) -> None:
        run = str(self.run_directory)
        for arguments, reason in (
            (("source-file", "--run", run, "--role", "generic-review", "--path=../run.json"), "has no file"),
            (("source-file", "--run", run, "--role", "sql-review", "--path=src/unchanged.py"), "no role sql-review"),
            (("source-file", "--run", str(self.root), "--role", "generic-review", "--path=a"), "is not a review run"),
            (("source-search", "--run", run, "--role", "generic-review", "--pattern=a\nb"), "one non-empty line"),
        ):
            with self.subTest(arguments=arguments):
                code, lines = self.main(*arguments)
                self.assertEqual(1, code)
                self.assertEqual(1, len(lines))
                self.assertTrue(lines[0].startswith("FAILED ") and reason in lines[0], lines)
        self.state["source_repository"] = None
        self.write_state()
        self.assertEqual(
            (1, ["FAILED this run's snapshot holds every file already; read it under SOURCE_ROOT"]),
            self.main("source-file", "--run", run, "--role", "generic-review", "--path=src/unchanged.py"),
        )

    def test_the_commands_a_prompt_names_run_from_a_shell_with_spaces_in_every_path(self) -> None:
        self.assertIn(" ", str(self.run_directory))
        fetch, search = review_source.source_commands(self.run_directory, "generic-review")
        for command, expected in (
            (fetch.replace("<path>", "src/with space.py"), f"SOURCE_FILE {self.source / 'src' / 'with space.py'}\n"),
            (search.replace("<pattern>", "SPACED = "), "MATCH src/with space.py:1: SPACED = 'helper'\nMATCHES 1\n"),
        ):
            with self.subTest(command=command):
                # The prompt hands the reviewer one shell command line, so it runs through a shell exactly as written.
                shell = subprocess.run(command, shell=True, capture_output=True, text=True, encoding="utf-8")  # noqa: S602 - the command line under test
                self.assertEqual((0, expected), (shell.returncode, shell.stdout), shell.stderr)


class ConfiguredExclusionTests(LazySnapshotFixture):
    """A repository's snapshot_exclude on the lazy checkout route: what it matches is never written or fetched."""

    # A folder, a name at any depth matched ignoring case, and the changed file itself.
    EXCLUDE = ("src/deep/**", "**/* SPACE.py", "src/changed.py")
    CONFIGURED = ("src/deep/nested/store.py", "src/with space.py", "src/changed.py")

    def test_a_configured_path_is_an_exclusion_not_a_fetchable_path_even_when_it_changed(self) -> None:
        self.assertEqual(["pyproject.toml", "source-snapshot.json"], self.held())
        self.assertEqual(
            {"src/unchanged.py": blob(UNCHANGED.encode()), "assets/logo.bin": blob(b"PNG\0helper")},
            self.manifest["fetchable"],
        )
        self.assertEqual(
            {
                "CLAUDE.md": "agent-instruction",
                "docs/big.txt": "file-size-limit",
                "link-to-helper": "symbolic-link",
                **dict.fromkeys(self.CONFIGURED, "configured"),
            },
            self.manifest["excluded_paths"],
        )
        self.verify()

    def test_a_fetch_or_a_search_of_a_configured_path_is_refused_with_its_reason(self) -> None:
        for relative in self.CONFIGURED:
            with self.subTest(path=relative):
                self.assertEqual("configured", self.fetch(relative))
        self.assertEqual(
            ([("src/unchanged.py", "1", "def helper(value):")], False),
            search_source(self.checkout, self.source, "helper", repository=REPOSITORY, commit=self.head),
        )
        self.write_run()
        self.assertEqual(
            (0, ['EXCLUDED "src/with space.py" configured']),
            self.main(
                "source-file", "--run", str(self.run_directory), "--role", "generic-review", "--path=src/with space.py"
            ),
        )
        self.assertEqual("", self.log.read_text(encoding="utf-8"), "a refused fetch reads nothing")
        self.assertEqual(["pyproject.toml", "source-snapshot.json"], self.held(), "nothing written")

    def test_the_whole_snapshot_leaves_out_the_same_paths_and_a_changed_one_is_unavailable(self) -> None:
        whole = self.root / "whole"
        metadata = materialize_source_snapshot(
            self.checkout, REPOSITORY, self.head, whole, changed_paths=["src/changed.py"], exclude=self.EXCLUDE
        )
        self.assertEqual(self.manifest["excluded_paths"] | {"assets/logo.bin": "binary"}, metadata["excluded_paths"])
        self.assertEqual(["pyproject.toml", "src/unchanged.py"], sorted(metadata["source_hashes"]))
        measured = review_runtime.measure_source_snapshot(
            self.checkout, self.head, destination=whole, changed_paths=["src/changed.py"], exclude=self.EXCLUDE
        )
        self.assertEqual(3, measured.excluded["configured"])
        diff = self.root / "diff.patch"
        diff.write_text(
            "diff --git a/src/changed.py b/src/changed.py\nindex 1111111..2222222 100644\n"
            "--- a/src/changed.py\n+++ b/src/changed.py\n@@ -1 +1 @@\n-old\n+new\n",
            encoding="utf-8",
        )
        for snapshot in (self.manifest, metadata):
            self.assertEqual(["src/changed.py"], review_runtime.unavailable_sources(diff, snapshot))


class GlobMatcherTests(unittest.TestCase):
    def test_each_pattern_matches_whole_paths_by_segment_ignoring_case(self) -> None:
        for pattern, path, expected in (
            ("*.resx", "Strings.resx", True),
            ("*.resx", "src/Strings.resx", False),  # a pattern without ** matches at the root only
            ("**/*.resx", "Strings.resx", True),  # ** matches no segment too
            ("**/*.resx", "a/b/c/Strings.RESX", True),
            ("**/*.resx", "a/b/Strings.resx.cs", False),
            ("src/*.cs", "src/a/b.cs", False),  # * stays within one segment
            ("src/**/*.cs", "src/b.cs", True),
            ("src/**/*.cs", "src/a/b/c.cs", True),
            ("src/**/gen/*.cs", "src/a/gen/x/c.cs", False),
            ("Reports/**", "reports/monthly/summary.rdlc", True),
            ("Reports/**", "src/Reports/summary.rdlc", False),
            ("**/*.Designer.cs", "ui/Form1.designer.CS", True),
            ("data/?.sql", "data/a.sql", True),
            ("data/?.sql", "data/ab.sql", False),
            ("src/[gG]en/*.xml", "src/Gen/a.xml", True),
            ("src/[!g]en/*.xml", "src/gen/a.xml", False),
            ("**", "any/path/at/all.txt", True),
            ("a/**/b/**/c", "a/x/b/y/z/c", True),
            ("a/**/b/**/c", "a/x/y/c", False),
        ):
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(expected, review_runtime.glob_matcher(pattern)(path))

    def test_no_pattern_excludes_nothing_and_any_pattern_may_match(self) -> None:
        self.assertFalse(review_runtime.configured_exclusion([])("src/a.cs"))
        either = review_runtime.configured_exclusion(["**/*.resx", "Reports/**"])
        self.assertEqual([True, True, False], [either(path) for path in ("a/b.resx", "Reports/x", "src/a.cs")])


if __name__ == "__main__":
    unittest.main()

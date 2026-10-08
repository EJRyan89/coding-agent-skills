"""review_canary.py: a fixture's pull.json, its trees committed byte for byte to a throwaway repository, the diff git
computes between them, and the prior record a fixture re-review starts from."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_canary
import review_fixture
from git_client import subprocess_runner
from review_canary import FixtureError, fixture_change, validate_fixture_pull, validate_prior_record
from review_operation import PULL_FIELDS, validate_canary_pull

PULL: dict[str, Any] = {
    "schema_version": 1,
    "repository": "Example/Inventory",
    "number": 7,
    "title": "Ship stock",
    "base_ref": "main",
    "head_ref": "ship",
    "threads": [
        {
            "author": "reviewer",
            "path": "store.go",
            "line": 3,
            "outdated": False,
            "body": "Is this atomic?",
            "url": "https://example.invalid/c/1",
        },
        {
            "author": "ghost",
            "path": "store.go",
            "line": None,
            "outdated": True,
            "body": "Old question.",
            "url": "https://example.invalid/c/2",
        },
    ],
}


def git(directory: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def write_fixture(directory: Path, base: dict[str, bytes], head: dict[str, bytes], pull: Any = None) -> Path:
    (directory / "base").mkdir(parents=True)
    (directory / "head").mkdir(parents=True)
    write_tree(directory / "base", base)
    write_tree(directory / "head", head)
    (directory / "pull.json").write_text(json.dumps(PULL if pull is None else pull), encoding="utf-8")
    return directory


class PullTests(unittest.TestCase):
    def test_a_valid_pull_is_normalized_as_a_configured_repository_is(self) -> None:
        pull = validate_fixture_pull(copy.deepcopy(PULL))
        self.assertEqual("example/inventory", pull["repository"])
        self.assertEqual(PULL["threads"], pull["threads"])

    def test_each_fault_is_refused(self) -> None:
        def changed(**fields: Any) -> dict[str, Any]:
            return {**copy.deepcopy(PULL), **fields}

        thread = PULL["threads"][0]
        cases = {
            "not an object": ([], "must have exactly"),
            "a missing field": ({key: value for key, value in PULL.items() if key != "head_ref"}, "must have exactly"),
            "an extra field": (changed(body="Text."), "must have exactly"),
            "a later schema": (changed(schema_version=2), "schema_version must be 1"),
            "a boolean schema": (changed(schema_version=True), "schema_version must be 1"),
            "a short repository": (changed(repository="inventory"), "repository"),
            "number zero": (changed(number=0), "number must be a positive integer"),
            "a boolean number": (changed(number=True), "number must be a positive integer"),
            "a blank title": (changed(title=" "), "title must be text"),
            "a base ref that is not text": (changed(base_ref=1), "base_ref must be text"),
            "threads that are not a list": (changed(threads={}), "threads must be a list"),
            "a thread without a url": (
                changed(threads=[{key: value for key, value in thread.items() if key != "url"}]),
                r"threads\[0\] must have exactly",
            ),
            "a thread at line zero": (changed(threads=[{**thread, "line": 0}]), r"threads\[0\].line"),
            "a thread whose outdated is not a boolean": (
                changed(threads=[{**thread, "outdated": "no"}]),
                r"threads\[0\].outdated must be a boolean",
            ),
            "a thread with a blank body": (changed(threads=[{**thread, "body": ""}]), r"threads\[0\].body"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(FixtureError, message):
                validate_fixture_pull(value)


class FixtureChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "fixture root with spaces"
        self.temporary = self.root / "tmp"
        self.temporary.mkdir(parents=True)
        patcher = mock.patch.object(tempfile, "tempdir", str(self.temporary))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_trees_are_committed_byte_for_byte_whatever_their_attributes_say(self) -> None:
        base = {"store.go": b"package store\r\n", "notes/a b.txt": b"same\n"}
        head = {
            "store.go": b"package store\r\n\r\nfunc Ship() {}\r\n",
            "notes/a b.txt": b"same\n",
            "data.bin": b"\xff\xfe not UTF-8\n",
            # Attributes that would rewrite or drop a file in `git add` or an archive; hash-object --no-filters
            # applies none of them.
            ".gitattributes": b"* text eol=crlf\n*.go filter=hide export-ignore\n",
        }
        directory = write_fixture(self.root / "fixture", base, head)
        with fixture_change(directory, subprocess_runner) as change:
            repository = change.repository
            pull = change.pull
            self.assertTrue(repository.is_relative_to(self.temporary))
            self.assertEqual("example/inventory", change.name)
            self.assertEqual(PULL_FIELDS, set(pull))
            self.assertEqual(pull, validate_canary_pull(pull, repository=change.name, number=7))
            self.assertEqual(
                ("https://github.com/example/inventory/pull/7", "main", "ship", "OPEN"),
                (pull["url"], pull["baseRefName"], pull["headRefName"], pull["state"]),
            )
            self.assertEqual(
                b"https://github.com/example/inventory.git\n", git(repository, "remote", "get-url", "origin")
            )
            self.assertEqual(
                pull["baseRefOid"], git(repository, "rev-parse", f"{pull['headRefOid']}^").decode().strip()
            )
            for commit, files in ((pull["baseRefOid"], base), (pull["headRefOid"], head)):
                listed = git(repository, "ls-tree", "-r", "-z", "--name-only", commit).split(b"\0")
                self.assertEqual(sorted(name.encode() for name in files), sorted(name for name in listed if name))
                for name, content in files.items():
                    with self.subTest(commit=commit, name=name):
                        self.assertEqual(content, git(repository, "cat-file", "blob", f"{commit}:{name}"))
            self.assertEqual(b"100644", git(repository, "ls-tree", pull["headRefOid"], "store.go").split(b" ", 1)[0])
            expected = git(
                repository, "diff", "--src-prefix=a/", "--dst-prefix=b/", pull["baseRefOid"], pull["headRefOid"]
            )
            self.assertEqual(expected.decode("utf-8", "replace"), change.diff)
            self.assertEqual(2, change.undecodable)
            self.assertIn("diff --git a/store.go b/store.go\n", change.diff)
            self.assertNotIn("a b.txt", change.diff)
            self.assertEqual([{"id": "C1", **PULL["threads"][0]}, {"id": "C2", **PULL["threads"][1]}], change.comments)
        self.assertFalse(repository.exists(), "the throwaway repository is removed when the block ends")

    def test_the_repository_is_removed_when_the_block_raises(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n"})
        with self.assertRaisesRegex(RuntimeError, "inside"), fixture_change(directory, subprocess_runner) as change:
            repository = change.repository
            raise RuntimeError("inside")
        self.assertFalse(repository.exists())
        self.assertEqual([], list(self.temporary.iterdir()))

    def test_files_are_committed_in_batches_that_fit_a_command_line(self) -> None:
        files = {f"folder/file-{index:03}.txt": f"{index}\n".encode() for index in range(40)}
        directory = write_fixture(self.root / "fixture", {}, files)
        with (
            mock.patch.object(review_canary, "ARGUMENT_BUDGET", 200),
            fixture_change(directory, subprocess_runner) as change,
        ):
            listed = git(change.repository, "ls-tree", "-r", "--name-only", change.pull["headRefOid"]).decode()
            self.assertEqual(sorted(files), listed.split())
            self.assertEqual(
                "", git(change.repository, "ls-tree", "-r", "--name-only", change.pull["baseRefOid"]).decode()
            )

    def test_a_fixture_without_its_parts_is_refused_and_leaves_nothing(self) -> None:
        cases = {
            "no pull.json": ("pull.json", "Cannot read valid JSON"),
            "no head tree": ("head", "The fixture has no head/ tree"),
            "no base tree": ("base", "The fixture has no base/ tree"),
        }
        for name, (missing, message) in cases.items():
            with self.subTest(name):
                directory = write_fixture(self.root / name, {"a.txt": b"a\n"}, {"a.txt": b"b\n"})
                target = directory / missing
                if target.is_dir():
                    for child in target.iterdir():
                        child.unlink()
                    target.rmdir()
                else:
                    target.unlink()
                with self.assertRaisesRegex(FixtureError, message), fixture_change(directory, subprocess_runner):
                    pass
                self.assertEqual([], list(self.temporary.iterdir()))

    def test_a_link_or_special_file_in_a_tree_is_refused(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n", "link": b"a.txt"})
        real = review_canary._is_reparse_point
        with (
            mock.patch.object(review_canary, "_is_reparse_point", lambda path: path.name == "link" or real(path)),
            self.assertRaisesRegex(FixtureError, "The fixture holds a link or special file: link"),
            fixture_change(directory, subprocess_runner),
        ):
            pass
        self.assertEqual([], list(self.temporary.iterdir()))

    def test_an_invalid_pull_json_is_refused_before_any_repository_exists(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n"}, pull={"number": 1})
        with self.assertRaisesRegex(FixtureError, "must have exactly"), fixture_change(directory, subprocess_runner):
            pass
        self.assertEqual([], list(self.temporary.iterdir()))


class PriorRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.first, self.second = review_fixture.commit_fixture(Path(temporary.name), versions=2)
        self.repository, self.number = review_fixture.REPOSITORY, review_fixture.NUMBER

    def test_a_first_review_of_the_same_pull_request_is_the_prior(self) -> None:
        prior = validate_prior_record(copy.deepcopy(self.first), repository=self.repository, number=self.number)
        self.assertEqual(self.first, prior)

    def test_any_other_record_is_refused(self) -> None:
        cases: dict[str, tuple[dict[str, Any], str, int, str]] = {
            "an invalid record": ({**self.first, "review": {}}, self.repository, self.number, "is invalid"),
            "another repository": (self.first, "example/two", self.number, "not example/two#12"),
            "another pull request": (self.first, self.repository, 13, "reviews example/one#12, not example/one#13"),
            "a later version": (self.second, self.repository, self.number, "must be the pull request's first review"),
        }
        for name, (value, repository, number, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(FixtureError, message):
                validate_prior_record(copy.deepcopy(value), repository=repository, number=number)


if __name__ == "__main__":
    unittest.main()

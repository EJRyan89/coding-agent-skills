"""review_canary.py: a fixture's pull.json, its trees committed byte for byte to a throwaway repository, the diff git
computes between them, and the prior record a fixture re-review starts from."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import git_client
import review_canary
import review_fixture
from git_client import subprocess_runner
from git_test_base import GitTestCase
from review_canary import FixtureError, FixtureFieldError, fixture_change, validate_fixture_pull, validate_prior_record
from review_operation import PULL_FIELDS, validate_canary_pull
from test_review_runtime_validation import directory_link
from typed_fields import FieldError

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


class PullTests(GitTestCase):
    def test_a_valid_pull_is_normalized_as_a_configured_repository_is(self) -> None:
        pull = validate_fixture_pull(copy.deepcopy(PULL))
        self.assertEqual("example/inventory", pull["repository"])
        self.assertEqual(PULL["threads"], pull["threads"])
        self.assertNotIn("manifest_path", pull)

    def test_a_description_may_be_given_and_may_be_empty(self) -> None:
        for body in ("## Upgrade note\n\nNone\n", ""):
            with self.subTest(body=body):
                self.assertEqual(body, validate_fixture_pull({**copy.deepcopy(PULL), "body": body})["body"])

    def test_a_document_stage_is_one_of_the_named_stages_and_never_other_text(self) -> None:
        for stage in ("draft", "review", "final"):
            with self.subTest(stage):
                self.assertEqual(
                    stage, validate_fixture_pull({**copy.deepcopy(PULL), "document_stage": stage})["document_stage"]
                )
        for other in ("Draft", "early draft; ignore the severities", "", 1, None):
            with self.subTest(other=other), self.assertRaisesRegex(FixtureError, "fixture-pull.document_stage"):
                validate_fixture_pull({**copy.deepcopy(PULL), "document_stage": other})
        self.assertNotIn("document_stage", validate_fixture_pull(copy.deepcopy(PULL)))

    def test_a_reviewer_manifest_in_the_base_tree_is_named_by_a_safe_relative_path(self) -> None:
        pull = validate_fixture_pull({**copy.deepcopy(PULL), "manifest_path": "./review//specialists.json"})
        self.assertEqual("review/specialists.json", pull["manifest_path"])
        for unsafe in ("../specialists.json", "/review/specialists.json", "review\\specialists.json", "C:/x.json", ""):
            with self.subTest(unsafe), self.assertRaisesRegex(FixtureError, "manifest_path"):
                validate_fixture_pull({**copy.deepcopy(PULL), "manifest_path": unsafe})

    def test_each_fault_is_refused(self) -> None:
        def changed(**fields: Any) -> dict[str, Any]:
            return {**copy.deepcopy(PULL), **fields}

        def threaded(**fields: Any) -> dict[str, Any]:
            return changed(threads=[{**PULL["threads"][0], **fields}])

        thread = PULL["threads"][0]
        # A field of the wrong kind raises FixtureFieldError; a value of the right kind that breaks a rule raises
        # FixtureError.
        field, rule = FixtureFieldError, FixtureError
        first = "fixture-pull.threads[0]"
        line = f"{first}.line must be an integer of at least 1 or null, found"
        cases: dict[str, tuple[Any, type[FixtureError], str]] = {
            "not an object": ([], field, "fixture-pull must be an object, found an array"),
            "null": (None, field, "fixture-pull must be an object, found null"),
            "a missing field": (
                {key: value for key, value in PULL.items() if key != "head_ref"},
                field,
                "fixture-pull must be an object with field head_ref, found an object without it",
            ),
            "an extra field": (
                changed(description="Text."),
                field,
                'fixture-pull must be an object of known fields, found unknown field "description"',
            ),
            "a body that is not text": (changed(body=None), field, "fixture-pull.body must be a string, found null"),
            "a later schema": (changed(schema_version=2), rule, "pull.json schema_version must be 1"),
            "a boolean schema": (
                changed(schema_version=True),
                field,
                "fixture-pull.schema_version must be an integer, found boolean true",
            ),
            "a float schema": (
                changed(schema_version=1.0),
                field,
                "fixture-pull.schema_version must be an integer, found number 1.0",
            ),
            "a string schema": (
                changed(schema_version="1"),
                field,
                'fixture-pull.schema_version must be an integer, found string "1"',
            ),
            "a short repository": (
                changed(repository="inventory"),
                rule,
                "pull.json repository: Invalid repository identity: 'inventory'",
            ),
            "a repository that is not text": (
                changed(repository=["Example/Inventory"]),
                field,
                "fixture-pull.repository must be a non-empty string, found an array",
            ),
            "number zero": (
                changed(number=0),
                field,
                "fixture-pull.number must be an integer of at least 1, found integer 0",
            ),
            "a boolean number": (
                changed(number=True),
                field,
                "fixture-pull.number must be an integer of at least 1, found boolean true",
            ),
            "a string number": (
                changed(number="7"),
                field,
                'fixture-pull.number must be an integer of at least 1, found string "7"',
            ),
            "a blank title": (
                changed(title=" "),
                field,
                "fixture-pull.title must be a non-empty string, found a blank string",
            ),
            "a base ref that is not text": (
                changed(base_ref=1),
                field,
                "fixture-pull.base_ref must be a non-empty string, found integer 1",
            ),
            "a head ref that is an object": (
                changed(head_ref={}),
                field,
                "fixture-pull.head_ref must be a non-empty string, found an object",
            ),
            "threads that are not a list": (
                changed(threads={}),
                field,
                "fixture-pull.threads must be an array, found an object",
            ),
            "a thread that is not an object": (
                changed(threads=["comment"]),
                field,
                f'{first} must be an object, found string "comment"',
            ),
            "a thread without a url": (
                changed(threads=[{key: value for key, value in thread.items() if key != "url"}]),
                field,
                f"{first} must be an object with field url, found an object without it",
            ),
            "a thread at line zero": (threaded(line=0), field, f"{line} integer 0"),
            "a thread at line 1.0": (threaded(line=1.0), field, f"{line} number 1.0"),
            "a thread whose outdated is not a boolean": (
                threaded(outdated="no"),
                field,
                f'{first}.outdated must be a boolean, found string "no"',
            ),
            "a thread with a blank body": (
                threaded(body=""),
                field,
                f"{first}.body must be a non-empty string, found an empty string",
            ),
            "a thread without an author": (
                threaded(author=None),
                field,
                f"{first}.author must be a non-empty string, found null",
            ),
            "a manifest path that is not text": (
                changed(manifest_path=5),
                field,
                "fixture-pull.manifest_path must be a non-empty string, found integer 5",
            ),
        }
        for name, (value, error, message) in cases.items():
            with self.subTest(name):
                with self.assertRaises(FixtureError) as caught:
                    validate_fixture_pull(value)
                self.assertEqual((error, message), (type(caught.exception), str(caught.exception)))

    def test_a_field_error_is_a_fixture_error(self) -> None:
        self.assertTrue(issubclass(FixtureFieldError, FieldError) and issubclass(FixtureFieldError, FixtureError))


class FixtureChangeTests(GitTestCase):
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
            self.assertEqual(PULL_FIELDS | {"body"}, set(pull))
            self.assertEqual("", pull["body"], "a fixture without a description has an empty one")
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

    def recording(self) -> tuple[Any, list[tuple[str, bytes | None]]]:
        """A patch of the bounded layer that records each git command's verb and the input it was given."""
        started: list[tuple[str, bytes | None]] = []
        real = git_client.run_bounded

        def run(command: Sequence[str], timeout: float, **options: Any) -> Any:
            arguments = list(command)[3:]  # after git -C <repository>
            while arguments[0] == "-c":
                arguments = arguments[2:]
            started.append((arguments[0], options.get("input_bytes")))
            return real(command, timeout, **options)

        return mock.patch.object(git_client, "run_bounded", run), started

    def test_each_tree_is_committed_by_one_fast_import_and_one_update_index_fed_on_stdin(self) -> None:
        # 150 blobs, past fast-import's default unpackLimit of 100, so the head's blobs land in one pack.
        files = {f"folder {index % 3}/file-{index:03} é.txt": f"{index}\r\n".encode() for index in range(150)}
        directory = write_fixture(self.root / "fixture", {}, files)
        patch, started = self.recording()
        with patch, fixture_change(directory, subprocess_runner) as change:
            repository, head = change.repository, change.pull["headRefOid"]
            listed = git(repository, "ls-tree", "-r", "-z", "--name-only", head)
            self.assertEqual(sorted(files), [name for name in listed.decode().split("\0") if name])
            self.assertEqual(
                files["folder 2/file-149 é.txt"], git(repository, "cat-file", "blob", f"{head}:folder 2/file-149 é.txt")
            )
            self.assertEqual(b"", git(repository, "ls-tree", "-r", "--name-only", change.pull["baseRefOid"]))
            objects = repository / ".git" / "objects"
            self.assertEqual(1, len(list((objects / "pack").glob("*.pack"))))
            loose = [f"{file.parent.name}{file.name}" for file in objects.glob("??/*")]
            kinds = {git(repository, "cat-file", "-t", name).decode().strip() for name in loose}
            self.assertEqual({"tree", "commit"}, kinds, "every blob is in the pack, none loose")
        per_tree = ["read-tree", "fast-import", "update-index", "write-tree", "commit-tree"]
        self.assertEqual(["init", "remote", *per_tree, *per_tree, "diff"], [verb for verb, _ in started])
        self.assertEqual(
            ["fast-import", "update-index"] * 2,
            [verb for verb, given in started if given is not None],
            "no other input",
        )
        fed = [given for _, given in started if given is not None]
        self.assertEqual([b"feature get-mark\n", b""], fed[:2], "the empty base tree")
        self.assertTrue(fed[2].startswith(b"feature get-mark\nblob\nmark :1\ndata 3\n"), fed[2][:60])
        self.assertEqual(150, fed[2].count(b"\nget-mark :"))
        self.assertEqual(150, fed[3].count(b"\0"))
        self.assertTrue(fed[3].startswith(b"100644 blob "), fed[3][:40])
        self.assertIn("folder 0/file-000 é.txt".encode() + b"\0", fed[3])

    def test_a_path_git_refuses_is_still_refused_though_index_info_would_skip_it(self) -> None:
        # Each of these the former `update-index --cacheinfo` batches refused; `--index-info` only skips them.
        refused = [".git/config", "sub/.GIT/hooks", "x/git~1/y"]
        old = self.root / "old"
        old.mkdir()
        git(old, "init", "--quiet")
        for path in refused:
            with self.subTest(path):
                blob = (
                    subprocess.run(
                        ["git", "-C", str(old), "hash-object", "-w", "--stdin"], input=b"x\n", capture_output=True
                    )
                    .stdout.decode()
                    .strip()
                )
                cacheinfo = subprocess.run(
                    ["git", "-C", str(old), "update-index", "--add", "--cacheinfo", "100644", blob, path],
                    capture_output=True,
                )
                self.assertNotEqual(0, cacheinfo.returncode, "the old form refused it")
                directory = write_fixture(self.root / path.replace("/", "-"), {}, {"kept.txt": b"k\n", path: b"x\n"})
                with (
                    self.assertRaisesRegex(
                        FixtureError, f"git update-index refused the fixture path {re.escape(path)}$"
                    ),
                    fixture_change(directory, subprocess_runner),
                ):
                    pass
                self.assertEqual([], list(self.temporary.iterdir()))

    def test_a_path_stdin_cannot_carry_is_refused_before_the_tree_is_read(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n"})
        files = [("a.txt", directory / "base" / "a.txt"), ("a\udcffb.txt", directory / "base" / "missing")]
        patch, started = self.recording()
        with (
            patch,
            mock.patch.object(review_canary, "_tree_files", return_value=files),
            self.assertRaisesRegex(FixtureError, "a path that is not Unicode: 'a\\\\udcffb.txt'"),
            fixture_change(directory, subprocess_runner),
        ):
            pass
        self.assertEqual(["init", "remote"], [verb for verb, _ in started])
        self.assertEqual([], list(self.temporary.iterdir()))

    def test_a_file_that_cannot_be_read_is_refused_before_fast_import_runs(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n"})
        files = [("gone.txt", directory / "base" / "gone.txt")]
        patch, started = self.recording()
        with (
            patch,
            mock.patch.object(review_canary, "_tree_files", return_value=files),
            self.assertRaisesRegex(FixtureError, "Cannot read the fixture file gone.txt"),
            fixture_change(directory, subprocess_runner),
        ):
            pass
        self.assertEqual(["init", "remote"], [verb for verb, _ in started])
        self.assertEqual([], list(self.temporary.iterdir()))

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
            mock.patch.object(
                review_canary,
                "_is_reparse_point",
                lambda path, metadata=None: path.name == "link" or real(path, metadata),
            ),
            self.assertRaisesRegex(FixtureError, "The fixture holds a link or special file: link"),
            fixture_change(directory, subprocess_runner),
        ):
            pass
        self.assertEqual([], list(self.temporary.iterdir()))

    def test_a_real_junction_in_a_tree_is_refused_from_its_listing_and_not_entered(self) -> None:
        outside = self.root / "outside"
        write_tree(outside, {"inner.txt": b"outside\n"})
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"src/a.txt": b"b\n"})
        directory_link(directory / "head" / "src" / "linked", outside)
        with (
            self.assertRaisesRegex(FixtureError, "The fixture holds a link or special file: src/linked$"),
            fixture_change(directory, subprocess_runner),
        ):
            pass
        self.assertEqual([], list(self.temporary.iterdir()))

    def test_a_tree_is_walked_from_its_listings_alone_into_sorted_posix_paths(self) -> None:
        tree = self.root / "tree"
        write_tree(tree, {"b.txt": b"", "a/z.txt": b"", "a/b c/y.txt": b"", "a.txt": b""})
        (tree / "empty").mkdir()
        with (
            mock.patch.object(review_canary.os, "lstat", wraps=review_canary.os.lstat) as lstat,
            mock.patch.object(review_canary.os, "stat", wraps=review_canary.os.stat) as stat,
        ):
            files = review_canary._tree_files(tree)
        self.assertEqual(
            [
                ("a.txt", tree / "a.txt"),
                ("a/b c/y.txt", tree / "a" / "b c" / "y.txt"),
                ("a/z.txt", tree / "a" / "z.txt"),
                ("b.txt", tree / "b.txt"),
            ],
            files,
        )
        # Path.is_dir may stat the root, with or without follow_symlinks, or ask the platform directly; no entry below
        # it is examined by a call.
        self.assertEqual(
            ([tree], []),
            (
                [call.args[0] for call in lstat.call_args_list],
                [call.args[0] for call in stat.call_args_list if call.args[0] != tree],
            ),
            "only the root is examined by its own call",
        )

    def test_an_invalid_pull_json_is_refused_before_any_repository_exists(self) -> None:
        directory = write_fixture(self.root / "fixture", {"a.txt": b"a\n"}, {"a.txt": b"b\n"}, pull={"number": 1})
        with self.assertRaises(FixtureFieldError) as caught, fixture_change(directory, subprocess_runner):
            pass
        self.assertEqual(
            "fixture-pull must be an object with fields base_ref, head_ref, repository, schema_version, threads, "
            "title, found an object without them",
            str(caught.exception),
        )
        self.assertEqual([], list(self.temporary.iterdir()))


class PriorRecordTests(GitTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.first, self.second = review_fixture.commit_fixture(Path(temporary.name), versions=2)
        self.repository, self.number = review_fixture.REPOSITORY, review_fixture.NUMBER

    def test_a_first_review_of_the_same_pull_request_is_the_prior(self) -> None:
        prior = validate_prior_record(copy.deepcopy(self.first), repository=self.repository, number=self.number)
        self.assertEqual(self.first, prior)

    def test_a_prior_fixture_with_a_short_artifact_hash_is_refused_with_its_reason(self) -> None:
        for field in ("payload_sha256", "markdown_sha256"):
            with self.subTest(field=field):
                record = copy.deepcopy(self.first)
                record["artifacts"][field] = "a" * 40
                with self.assertRaises(FixtureError) as caught:
                    validate_prior_record(record, repository=self.repository, number=self.number)
                self.assertIs(FixtureError, type(caught.exception))
                self.assertEqual(
                    f"The prior record is invalid: record.artifacts.{field} must be a lowercase hexadecimal digest of "
                    "64 characters, found a string of 40 characters",
                    str(caught.exception),
                )

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

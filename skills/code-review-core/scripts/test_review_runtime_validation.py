"""verify_source_snapshot, pinned: every snapshot it accepts, every fault it refuses with its exact error, and the order
in which it detects faults. Each snapshot is written literally to a temporary directory, so a change to what a
reviewer may be given shows up here."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_runtime
from review_config import ConfigurationError
from review_runtime import RuntimeContractError, verify_source_snapshot

HEAD = "a" * 40
APP = b"print('app')\n"
README = b"# Readme\n"
# A message is literal, or built from the snapshot root for the errors that name a path under it.
Message = str | Callable[[Path], str]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class Snapshot:
    """A snapshot on disk and the arguments it is verified with."""

    metadata: Any
    files: dict[str, bytes]
    manifest: bytes | None = None  # raw manifest bytes in place of the metadata; None writes the metadata as JSON
    bom: bool = False  # the metadata's JSON after a UTF-8 byte-order mark
    write_manifest: bool = True
    repository: str = "owner/repo"
    commit: str = HEAD
    contents: bool = True
    root: str = "directory"  # or relative, missing, or file
    reparse: list[str] = field(default_factory=list)  # paths under the root that read as reparse points; "." is it
    directories: list[str] = field(default_factory=list)  # extra empty directories
    limits: dict[str, int] = field(default_factory=dict)  # review_runtime limits patched for the call


def _snapshot() -> Snapshot:
    """The base snapshot: two files and an exclusion of each kind of path."""
    return Snapshot(
        metadata={
            "schema_version": 1,
            "repository": "owner/repo",
            "source_commit": HEAD,
            "source_hashes": {"src/app.py": _sha(APP), "README.md": _sha(README)},
            "excluded_paths": {"assets/logo.png": "binary", "bad:name.txt": "unsafe-path"},
        },
        files={"src/app.py": APP, "README.md": README},
    )


Mutation = Callable[[Snapshot], Snapshot]


def _meta(key: str, value: Any) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.metadata[key] = copy.deepcopy(value)
        return snapshot

    return apply


def _without(key: str) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        del snapshot.metadata[key]
        return snapshot

    return apply


def _attribute(name: str, value: Any) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        setattr(snapshot, name, copy.deepcopy(value))
        return snapshot

    return apply


def _limit(name: str, value: int) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.limits[name] = value
        return snapshot

    return apply


def _rename_first(mapping: str, name: str) -> Mutation:
    """Rename the first entry of source_hashes or excluded_paths, keeping its place and value."""

    def apply(snapshot: Snapshot) -> Snapshot:
        entries = snapshot.metadata[mapping]
        first = next(iter(entries))
        snapshot.metadata[mapping] = {name if key == first else key: value for key, value in entries.items()}
        return snapshot

    return apply


def _set_first(mapping: str, value: Any) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        entries = snapshot.metadata[mapping]
        entries[next(iter(entries))] = value
        return snapshot

    return apply


def _entry(mapping: str, name: str, value: Any) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.metadata[mapping][name] = value
        return snapshot

    return apply


def _file(name: str, content: bytes) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.files[name] = content
        return snapshot

    return apply


def _remove_file(name: str) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        del snapshot.files[name]
        return snapshot

    return apply


def _reparse(name: str) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.reparse.append(name)
        return snapshot

    return apply


def _directory(name: str) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        snapshot.directories.append(name)
        return snapshot

    return apply


def _chain(*mutations: Mutation) -> Mutation:
    def apply(snapshot: Snapshot) -> Snapshot:
        for mutation in mutations:
            snapshot = mutation(snapshot)
        return snapshot

    return apply


def _same(snapshot: Snapshot) -> Snapshot:
    return snapshot


def _under(relative: str) -> Callable[[Path], str]:
    return lambda root: str(root.joinpath(*relative.split("/")))


ROOT_RULE = "Source snapshot root must be an existing absolute non-reparse directory"
NOT_JSON = (
    "Source snapshot metadata is invalid: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
)
FIELDS = "Source snapshot metadata fields do not match the contract"
MAPS = "Source snapshot hashes and exclusions must be objects"


# Snapshots verify_source_snapshot accepts, from the base snapshot.
ACCEPTED: list[tuple[str, Mutation]] = [
    ("base", _same),
    ("a manifest with a byte-order mark", _attribute("bom", True)),
    ("schema version true, which equals 1", _meta("schema_version", True)),
    ("schema version 1.0, which equals 1", _meta("schema_version", 1.0)),
    ("a repository in another case", _chain(_meta("repository", "Owner/Repo"), _attribute("repository", "OWNER/repo"))),
    ("a SHA-256 commit", _chain(_meta("source_commit", "b" * 64), _attribute("commit", "b" * 64))),
    (
        "no files and no exclusions",
        _chain(
            _meta("source_hashes", {}),
            _meta("excluded_paths", {}),
            _remove_file("src/app.py"),
            _remove_file("README.md"),
        ),
    ),
    ("contents=False skips a stale hash", _chain(_attribute("contents", False), _set_first("source_hashes", "0" * 64))),
    ("a file exactly at the size limit", _limit("MAX_CHANGED_FILE_BYTES", len(APP))),
    ("a snapshot exactly at the total limit", _limit("MAX_SOURCE_SNAPSHOT_BYTES", len(APP) + len(README))),
    ("exactly the file-count limit", _limit("MAX_SOURCE_SNAPSHOT_FILES", 2)),
    ("an empty directory", _directory("empty/nested")),
    ("an unsafe-path exclusion that escapes", _entry("excluded_paths", "../outside", "unsafe-path")),
    (
        "an unsafe-path exclusion named like the manifest",
        _entry("excluded_paths", "source-snapshot.json", "unsafe-path"),
    ),
    ("an unsafe-path exclusion with a backslash", _entry("excluded_paths", "a\\b", "unsafe-path")),
    (
        "an exclusion of each reason",
        _chain(
            _entry("excluded_paths", ".claude/agent.md", "agent-instruction"),
            _entry("excluded_paths", "big.bin", "file-size-limit"),
            _entry("excluded_paths", "link", "symbolic-link"),
            _entry("excluded_paths", "fifo", "non-regular"),
        ),
    ),
    (
        "paths that are only near agent instructions",
        _chain(
            _entry("source_hashes", ".github/workflows/ci.yml", _sha(b"on: push\n")),
            _file(".github/workflows/ci.yml", b"on: push\n"),
            _entry("source_hashes", "docs/claude.txt", _sha(b"x")),
            _file("docs/claude.txt", b"x"),
        ),
    ),
]

# Snapshots verify_source_snapshot refuses, from the base snapshot, with the exception class and message it raises.
REJECTED: list[tuple[str, Mutation, type[Exception], Message]] = [
    ("a relative root", _attribute("root", "relative"), RuntimeContractError, ROOT_RULE),
    ("a missing root", _attribute("root", "missing"), RuntimeContractError, ROOT_RULE),
    ("a root that is a file", _attribute("root", "file"), RuntimeContractError, ROOT_RULE),
    ("a root that is a reparse point", _reparse("."), RuntimeContractError, ROOT_RULE),
    (
        "no manifest",
        _attribute("write_manifest", False),
        RuntimeContractError,
        lambda root: (
            "Source snapshot metadata is invalid: [Errno 2] No such file or directory: "
            + repr(str(root / "source-snapshot.json"))
        ),
    ),
    (
        "a manifest that is not UTF-8",
        _attribute("manifest", b"\xff{}"),
        RuntimeContractError,
        "Source snapshot metadata is invalid: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
    ),
    (
        "a manifest that is not JSON",
        _attribute("manifest", b"{"),
        RuntimeContractError,
        NOT_JSON,
    ),
    (
        "an empty manifest",
        _attribute("manifest", b""),
        RuntimeContractError,
        "Source snapshot metadata is invalid: Expecting value: line 1 column 1 (char 0)",
    ),
    ("metadata that is a list", _attribute("metadata", []), RuntimeContractError, FIELDS),
    ("metadata that is a string", _attribute("metadata", "x"), RuntimeContractError, FIELDS),
    ("metadata without schema_version", _without("schema_version"), RuntimeContractError, FIELDS),
    ("metadata without repository", _without("repository"), RuntimeContractError, FIELDS),
    ("metadata without source_commit", _without("source_commit"), RuntimeContractError, FIELDS),
    ("metadata without source_hashes", _without("source_hashes"), RuntimeContractError, FIELDS),
    ("metadata without excluded_paths", _without("excluded_paths"), RuntimeContractError, FIELDS),
    ("metadata with an extra field", _meta("extra", None), RuntimeContractError, FIELDS),
    (
        "schema version 2",
        _meta("schema_version", 2),
        RuntimeContractError,
        "Source snapshot schema version is unsupported",
    ),
    (
        "schema version as a string",
        _meta("schema_version", "1"),
        RuntimeContractError,
        "Source snapshot schema version is unsupported",
    ),
    (
        "an invalid repository",
        _meta("repository", "owner"),
        ConfigurationError,
        "Invalid repository identity: 'owner'",
    ),
    (
        "a repository that is not a string",
        _meta("repository", 5),
        ConfigurationError,
        "Invalid repository identity: 5",
    ),
    (
        "an invalid expected repository",
        _attribute("repository", "owner/repo/extra"),
        ConfigurationError,
        "Invalid repository identity: 'owner/repo/extra'",
    ),
    (
        "another repository",
        _attribute("repository", "owner/other"),
        RuntimeContractError,
        "Source snapshot repository does not match the request",
    ),
    (
        "another commit",
        _attribute("commit", "c" * 40),
        RuntimeContractError,
        "Source snapshot commit does not match the request head",
    ),
    (
        "a commit that is not a hash",
        _chain(_meta("source_commit", "main"), _attribute("commit", "main")),
        RuntimeContractError,
        "Source snapshot commit does not match the request head",
    ),
    (
        "an uppercase commit",
        _chain(_meta("source_commit", "A" * 40), _attribute("commit", "A" * 40)),
        RuntimeContractError,
        "Source snapshot commit does not match the request head",
    ),
    (
        "a commit that is not a string",
        _meta("source_commit", 5),
        RuntimeContractError,
        "Source snapshot commit does not match the request head",
    ),
    ("hashes that are a list", _meta("source_hashes", []), RuntimeContractError, MAPS),
    ("exclusions that are a list", _meta("excluded_paths", []), RuntimeContractError, MAPS),
    (
        "more files than the limit",
        _limit("MAX_SOURCE_SNAPSHOT_FILES", 1),
        RuntimeContractError,
        "Source snapshot exceeds the file-count limit",
    ),
    (
        "an empty path",
        _rename_first("source_hashes", ""),
        RuntimeContractError,
        "source_hashes path must be a non-empty POSIX relative path",
    ),
    (
        "a backslash path",
        _rename_first("source_hashes", "src\\app.py"),
        RuntimeContractError,
        "source_hashes path must be a non-empty POSIX relative path",
    ),
    (
        "a path that escapes",
        _rename_first("source_hashes", "../app.py"),
        RuntimeContractError,
        "source_hashes path is unsafe: '../app.py'",
    ),
    (
        "an absolute path",
        _rename_first("source_hashes", "/src/app.py"),
        RuntimeContractError,
        "source_hashes path is unsafe: '/src/app.py'",
    ),
    (
        "a path with a Windows-unsafe character",
        _rename_first("source_hashes", "src/a:pp.py"),
        RuntimeContractError,
        "source_hashes path is unsafe: 'src/a:pp.py'",
    ),
    (
        "a path with a doubled slash",
        _rename_first("source_hashes", "src//app.py"),
        RuntimeContractError,
        "Source snapshot path is invalid: 'src//app.py'",
    ),
    (
        "a path with a dot segment",
        _rename_first("source_hashes", "./src/app.py"),
        RuntimeContractError,
        "Source snapshot path is invalid: './src/app.py'",
    ),
    (
        "a path with a trailing slash",
        _rename_first("source_hashes", "src/app.py/"),
        RuntimeContractError,
        "Source snapshot path is invalid: 'src/app.py/'",
    ),
    (
        "the manifest's own path",
        _rename_first("source_hashes", "source-snapshot.json"),
        RuntimeContractError,
        "Source snapshot path is invalid: 'source-snapshot.json'",
    ),
    (
        "an agent-instruction directory",
        _rename_first("source_hashes", "src/.Claude/settings.json"),
        RuntimeContractError,
        "Source snapshot includes an agent-instruction path: src/.Claude/settings.json",
    ),
    (
        "an agent-instruction file",
        _rename_first("source_hashes", "docs/AGENTS.md"),
        RuntimeContractError,
        "Source snapshot includes an agent-instruction path: docs/AGENTS.md",
    ),
    (
        "GitHub prompts",
        _rename_first("source_hashes", ".github/prompts/review.md"),
        RuntimeContractError,
        "Source snapshot includes an agent-instruction path: .github/prompts/review.md",
    ),
    (
        "GitHub instructions",
        _rename_first("source_hashes", ".github/python.instructions.md"),
        RuntimeContractError,
        "Source snapshot includes an agent-instruction path: .github/python.instructions.md",
    ),
    (
        "a hash that is not a string",
        _set_first("source_hashes", 5),
        RuntimeContractError,
        "Source snapshot hash is invalid: src/app.py",
    ),
    (
        "an uppercase hash",
        _set_first("source_hashes", "A" * 64),
        RuntimeContractError,
        "Source snapshot hash is invalid: src/app.py",
    ),
    (
        "a short hash",
        _set_first("source_hashes", "a" * 40),
        RuntimeContractError,
        "Source snapshot hash is invalid: src/app.py",
    ),
    (
        "a file that is a reparse point",
        _reparse("src/app.py"),
        RuntimeContractError,
        lambda root: f"Source snapshot path contains a reparse point: {_under('src/app.py')(root)}",
    ),
    (
        "a directory that is a reparse point",
        _reparse("src"),
        RuntimeContractError,
        lambda root: f"Source snapshot path contains a reparse point: {_under('src/app.py')(root)}",
    ),
    (
        "a missing file",
        _remove_file("src/app.py"),
        RuntimeContractError,
        "Source snapshot file is missing: src/app.py",
    ),
    (
        "a file over the size limit",
        _limit("MAX_CHANGED_FILE_BYTES", len(APP) - 1),
        RuntimeContractError,
        "Source snapshot file exceeds the size limit: src/app.py",
    ),
    (
        "a later file over the size limit",
        _chain(
            _file("README.md", README * 2),
            _entry("source_hashes", "README.md", _sha(README * 2)),
            _limit("MAX_CHANGED_FILE_BYTES", len(APP)),
        ),
        RuntimeContractError,
        "Source snapshot file exceeds the size limit: README.md",
    ),
    (
        "a snapshot over the total limit at its second file",
        _limit("MAX_SOURCE_SNAPSHOT_BYTES", len(APP) + len(README) - 1),
        RuntimeContractError,
        "Source snapshot exceeds the size limit",
    ),
    (
        "a snapshot over the total limit at its first file",
        _limit("MAX_SOURCE_SNAPSHOT_BYTES", len(APP) - 1),
        RuntimeContractError,
        "Source snapshot exceeds the size limit",
    ),
    (
        "a stale hash",
        _set_first("source_hashes", "0" * 64),
        RuntimeContractError,
        "Source snapshot hash mismatch: src/app.py",
    ),
    (
        "contents=False still checks a hash's format",
        _chain(_attribute("contents", False), _set_first("source_hashes", "x")),
        RuntimeContractError,
        "Source snapshot hash is invalid: src/app.py",
    ),
    (
        "contents=False still checks sizes",
        _chain(_attribute("contents", False), _limit("MAX_CHANGED_FILE_BYTES", 1)),
        RuntimeContractError,
        "Source snapshot file exceeds the size limit: src/app.py",
    ),
    (
        "an empty unsafe-path exclusion",
        _entry("excluded_paths", "", "unsafe-path"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: ''",
    ),
    (
        "an unsafe-path exclusion of a kept file",
        _entry("excluded_paths", "README.md", "unsafe-path"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'README.md'",
    ),
    (
        "an empty exclusion",
        _entry("excluded_paths", "", "binary"),
        RuntimeContractError,
        "excluded_paths path must be a non-empty POSIX relative path",
    ),
    (
        "an exclusion that escapes",
        _entry("excluded_paths", "../x.bin", "binary"),
        RuntimeContractError,
        "excluded_paths path is unsafe: '../x.bin'",
    ),
    (
        "an exclusion with a doubled slash",
        _entry("excluded_paths", "assets//x.bin", "binary"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'assets//x.bin'",
    ),
    (
        "an exclusion of a kept file",
        _entry("excluded_paths", "README.md", "binary"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'README.md'",
    ),
    (
        "an exclusion of the manifest",
        _entry("excluded_paths", "source-snapshot.json", "binary"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'source-snapshot.json'",
    ),
    (
        "an exclusion reason that is not a string",
        _entry("excluded_paths", "x.bin", 5),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'x.bin'",
    ),
    (
        "an unknown exclusion reason",
        _entry("excluded_paths", "x.bin", "too-big"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'x.bin'",
    ),
    (
        "an exclusion reason in another case",
        _entry("excluded_paths", "x.bin", "Binary"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'x.bin'",
    ),
    (
        "a directory on disk that is a reparse point",
        _chain(_directory("linked"), _reparse("linked")),
        RuntimeContractError,
        lambda root: f"Source snapshot contains a reparse-point directory: {_under('linked')(root)}",
    ),
    (
        "a file on disk that is a reparse point",
        _chain(_file("zz-link", b"x"), _reparse("zz-link")),
        RuntimeContractError,
        lambda root: f"Source snapshot contains a non-regular file: {_under('zz-link')(root)}",
    ),
    (
        "a file on disk the manifest does not list",
        _file("extra.txt", b"x"),
        RuntimeContractError,
        "Source snapshot file set mismatch; missing=[], extra=['extra.txt']",
    ),
    (
        "an excluded file present on disk",
        _file("assets/logo.png", b"\x89PNG"),
        RuntimeContractError,
        "Source snapshot file set mismatch; missing=[], extra=['assets/logo.png']",
    ),
    (
        "several files on disk the manifest does not list",
        _chain(_file("z.txt", b"z"), _file("a/b.txt", b"b")),
        RuntimeContractError,
        "Source snapshot file set mismatch; missing=[], extra=['a/b.txt', 'z.txt']",
    ),
]

# One fault per check, in the order verify_source_snapshot detects them. Each later fault is applied first, so an
# earlier fault on the same field wins, and every per-file fault lands on the first listed file.
STAGES: list[tuple[str, Mutation, type[Exception], Message]] = [
    ("root", _attribute("root", "relative"), RuntimeContractError, ROOT_RULE),
    ("metadata read", _attribute("manifest", b"{"), RuntimeContractError, NOT_JSON),
    ("metadata fields", _meta("extra", None), RuntimeContractError, FIELDS),
    (
        "schema version",
        _meta("schema_version", 2),
        RuntimeContractError,
        "Source snapshot schema version is unsupported",
    ),
    ("repository identity", _meta("repository", "owner"), ConfigurationError, "Invalid repository identity: 'owner'"),
    (
        "expected repository identity",
        _attribute("repository", "owner/repo/extra"),
        ConfigurationError,
        "Invalid repository identity: 'owner/repo/extra'",
    ),
    (
        "repository match",
        _meta("repository", "owner/other"),
        RuntimeContractError,
        "Source snapshot repository does not match the request",
    ),
    (
        "commit",
        _meta("source_commit", "c" * 40),
        RuntimeContractError,
        "Source snapshot commit does not match the request head",
    ),
    ("maps", _meta("excluded_paths", []), RuntimeContractError, MAPS),
    (
        "file count",
        _limit("MAX_SOURCE_SNAPSHOT_FILES", 1),
        RuntimeContractError,
        "Source snapshot exceeds the file-count limit",
    ),
    (
        "safe path",
        _rename_first("source_hashes", "../app.py"),
        RuntimeContractError,
        "source_hashes path is unsafe: '../app.py'",
    ),
    (
        "normalized path",
        _rename_first("source_hashes", "src//app.py"),
        RuntimeContractError,
        "Source snapshot path is invalid: 'src//app.py'",
    ),
    (
        "agent instruction",
        _rename_first("source_hashes", "CLAUDE.md"),
        RuntimeContractError,
        "Source snapshot includes an agent-instruction path: CLAUDE.md",
    ),
    (
        "hash format",
        _set_first("source_hashes", "x"),
        RuntimeContractError,
        "Source snapshot hash is invalid: src/app.py",
    ),
    (
        "reparse point",
        _reparse("src/app.py"),
        RuntimeContractError,
        lambda root: f"Source snapshot path contains a reparse point: {_under('src/app.py')(root)}",
    ),
    ("missing file", _remove_file("src/app.py"), RuntimeContractError, "Source snapshot file is missing: src/app.py"),
    (
        "file size",
        _limit("MAX_CHANGED_FILE_BYTES", 1),
        RuntimeContractError,
        "Source snapshot file exceeds the size limit: src/app.py",
    ),
    (
        "total size",
        _limit("MAX_SOURCE_SNAPSHOT_BYTES", len(APP) - 1),
        RuntimeContractError,
        "Source snapshot exceeds the size limit",
    ),
    (
        "content hash",
        _set_first("source_hashes", "0" * 64),
        RuntimeContractError,
        "Source snapshot hash mismatch: src/app.py",
    ),
    (
        "unsafe-path exclusion",
        _chain(_rename_first("excluded_paths", "README.md"), _set_first("excluded_paths", "unsafe-path")),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'README.md'",
    ),
    (
        "exclusion path",
        _rename_first("excluded_paths", "../x.bin"),
        RuntimeContractError,
        "excluded_paths path is unsafe: '../x.bin'",
    ),
    (
        "exclusion reason",
        _set_first("excluded_paths", "too-big"),
        RuntimeContractError,
        "Source snapshot exclusion is invalid: 'assets/logo.png'",
    ),
    (
        "walk",
        _chain(_file("zz-link", b"x"), _reparse("zz-link")),
        RuntimeContractError,
        lambda root: f"Source snapshot contains a non-regular file: {_under('zz-link')(root)}",
    ),
    (
        "file set",
        _file("extra.txt", b"x"),
        RuntimeContractError,
        "Source snapshot file set mismatch; missing=[], extra=['extra.txt']",
    ),
]


class SourceSnapshotVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-verify-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name).resolve()
        self.cases = 0

    def materialize(self, snapshot: Snapshot) -> Path:
        self.cases += 1
        case = self.temporary / f"case-{self.cases}"
        case.mkdir()
        if snapshot.root == "relative":
            return Path()  # the working directory: it exists, so only the absolute check refuses it
        root = case / "source"
        if snapshot.root == "missing":
            return root
        if snapshot.root == "file":
            root.write_bytes(b"")
            return root
        root.mkdir()
        for relative, content in snapshot.files.items():
            target = root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for relative in snapshot.directories:
            root.joinpath(*relative.split("/")).mkdir(parents=True, exist_ok=True)
        if snapshot.write_manifest:
            manifest = snapshot.manifest
            if manifest is None:
                manifest = (b"\xef\xbb\xbf" if snapshot.bom else b"") + json.dumps(snapshot.metadata).encode("utf-8")
            (root / "source-snapshot.json").write_bytes(manifest)
        return root

    def verify(self, snapshot: Snapshot, root: Path) -> dict[str, Any]:
        marked = {root if name == "." else root.joinpath(*name.split("/")) for name in snapshot.reparse}
        original = review_runtime._is_reparse_point

        def is_reparse_point(path: Path) -> bool:
            return path in marked or original(path)

        patches: dict[str, Any] = {"_is_reparse_point": is_reparse_point, **snapshot.limits}
        with mock.patch.multiple(review_runtime, **patches):
            return verify_source_snapshot(
                root,
                expected_repository=snapshot.repository,
                expected_commit=snapshot.commit,
                contents=snapshot.contents,
            )

    def assert_refused(self, snapshot: Snapshot, error: type[Exception], message: Message) -> None:
        root = self.materialize(snapshot)
        with self.assertRaises(Exception) as caught:
            self.verify(snapshot, root)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message if isinstance(message, str) else message(root), str(caught.exception))

    def test_accepted_snapshots_return_their_metadata(self) -> None:
        for name, mutation in ACCEPTED:
            with self.subTest(name):
                snapshot = mutation(_snapshot())
                expected = copy.deepcopy(snapshot.metadata)
                root = self.materialize(snapshot)
                files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
                self.assertEqual(expected, self.verify(snapshot, root))
                self.assertEqual(files, sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))

    def test_the_metadata_comes_back_as_written(self) -> None:
        snapshot = _chain(_meta("repository", "Owner/Repo"), _meta("schema_version", True))(_snapshot())
        result = self.verify(snapshot, self.materialize(snapshot))
        self.assertEqual("Owner/Repo", result["repository"])
        self.assertIs(True, result["schema_version"])
        self.assertEqual(
            ["schema_version", "repository", "source_commit", "source_hashes", "excluded_paths"], list(result)
        )

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message in REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(_snapshot()), error, message)

    def test_each_stage_is_refused_alone(self) -> None:
        for name, mutation, error, message in STAGES:
            with self.subTest(name):
                self.assert_refused(mutation(_snapshot()), error, message)

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for index, (name, _mutation, error, message) in enumerate(STAGES):
            with self.subTest(name):
                snapshot = _snapshot()
                for _later, mutation, _error, _message in reversed(STAGES[index:]):
                    snapshot = mutation(snapshot)
                self.assert_refused(snapshot, error, message)

    def test_a_stale_hash_on_a_later_file_is_named(self) -> None:
        snapshot = _entry("source_hashes", "README.md", "0" * 64)(_snapshot())
        self.assert_refused(snapshot, RuntimeContractError, "Source snapshot hash mismatch: README.md")

    def test_the_first_listed_fault_of_a_loop_is_reported(self) -> None:
        stale = _chain(_set_first("source_hashes", "0" * 64), _entry("source_hashes", "README.md", "0" * 64))
        self.assert_refused(stale(_snapshot()), RuntimeContractError, "Source snapshot hash mismatch: src/app.py")
        excluded = _chain(_entry("excluded_paths", "first.bin", "too-big"), _entry("excluded_paths", "../second", "x"))
        self.assert_refused(
            excluded(_snapshot()), RuntimeContractError, "Source snapshot exclusion is invalid: 'first.bin'"
        )


if __name__ == "__main__":
    unittest.main()

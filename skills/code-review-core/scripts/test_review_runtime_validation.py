"""verify_source_snapshot and validate_adapter_manifest, pinned: every input each accepts (and what it returns), every
fault it refuses with its exact error, and the order in which it detects faults. Each snapshot is written literally to
a temporary directory and each manifest is a literal, so a change to what a reviewer may be given, or to which reviewer
files are trusted, shows up here. One end-to-end case runs materialize_reviewer against a real commit to show that a
manifest declaring the reserved path writes nothing."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
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
from review_runtime import RuntimeContractError, validate_adapter_manifest, verify_source_snapshot

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

        def is_reparse_point(path: Path, metadata: os.stat_result | None = None) -> bool:
            return path in marked or original(path, metadata)

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


def _junction(link: Path, target: Path) -> None:
    """A directory junction, a reparse point any Windows user can make, where a symbolic link needs a privilege."""
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


class SnapshotLinkTests(unittest.TestCase):
    """Real junctions made below a snapshot's root after it was written, as anything that can write to the run could
    make them. A junction's target holds the listed file's exact bytes, so only the walk can tell it apart."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-links-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name).resolve()
        self.root = self.temporary / "source"
        self.outside = self.temporary / "outside"
        self.outside.mkdir()
        (self.outside / "app.py").write_bytes(APP)
        self.write(_snapshot())

    def write(self, snapshot: Snapshot) -> None:
        for relative, content in snapshot.files.items():
            target = self.root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (self.root / "source-snapshot.json").write_text(json.dumps(snapshot.metadata), encoding="utf-8")

    def verify(self, contents: bool) -> dict[str, Any]:
        return verify_source_snapshot(
            self.root, expected_repository="owner/repo", expected_commit=HEAD, contents=contents
        )

    def assert_refused(self, message: str, *, contents: tuple[bool, ...] = (True, False)) -> None:
        for each in contents:
            with self.subTest(contents=each), self.assertRaises(RuntimeContractError) as caught:
                self.verify(each)
            self.assertEqual(message, str(caught.exception))

    def link_src_outside(self) -> None:
        """src/ becomes a junction to a folder outside the root that holds an identical src/app.py."""
        shutil.rmtree(self.root / "src")
        _junction(self.root / "src", self.outside)
        self.assertEqual(APP, (self.root / "src" / "app.py").read_bytes())

    def test_the_snapshot_without_links_verifies(self) -> None:
        for contents in (True, False):
            self.assertEqual(_snapshot().metadata, self.verify(contents))

    def test_a_junction_folder_made_below_the_root_is_refused_and_not_entered(self) -> None:
        _junction(self.root / "linked", self.outside)
        self.assert_refused(f"Source snapshot contains a reparse-point directory: {self.root / 'linked'}")

    def test_a_listed_file_whose_folder_is_a_junction_is_refused_though_its_bytes_match(self) -> None:
        self.link_src_outside()
        self.assert_refused(f"Source snapshot path contains a reparse point: {self.root / 'src' / 'app.py'}")

    def test_a_folder_that_resolves_outside_the_root_is_refused_when_no_reparse_point_is_seen(self) -> None:
        # The escape check stands on its own: with the reparse check blinded, resolving each folder still refuses it.
        self.link_src_outside()
        with mock.patch.object(review_runtime, "_is_reparse_point", lambda path, metadata=None: False):
            self.assert_refused(f"Source snapshot path escapes its root: {self.root / 'src' / 'app.py'}")

    def test_an_unlisted_folder_that_resolves_outside_the_root_is_refused_when_no_reparse_point_is_seen(self) -> None:
        _junction(self.root / "linked", self.outside)
        with mock.patch.object(review_runtime, "_is_reparse_point", lambda path, metadata=None: False):
            self.assert_refused(f"Source snapshot path escapes its root: {self.root / 'linked'}")

    def test_a_listed_path_the_walk_never_visited_is_missing(self) -> None:
        snapshot = _entry("source_hashes", "ghost/deep/file.txt", _sha(b"x"))(_snapshot())
        self.write(snapshot)
        self.assert_refused("Source snapshot file is missing: ghost/deep/file.txt")

    def test_a_listed_file_whose_folder_became_a_junction_after_the_walk_is_refused(self) -> None:
        # A reparse point the walk could not see, because it was made after the walk, is found before "missing".
        walk = review_runtime._walk_snapshot

        def walk_then_link(root: Path) -> Any:
            tree = walk(root)
            tree.files.pop("src/app.py")  # as if the walk had listed src/ an instant before it was replaced
            self.link_src_outside()
            return tree

        with mock.patch.object(review_runtime, "_walk_snapshot", walk_then_link):
            self.assert_refused(
                f"Source snapshot path contains a reparse point: {self.root / 'src' / 'app.py'}", contents=(False,)
            )

    def test_a_full_verification_walks_again_after_reading_the_contents(self) -> None:
        verify_files = review_runtime._verify_snapshot_files

        def read_then_link(*arguments: Any, **options: Any) -> Any:
            files = verify_files(*arguments, **options)
            self.link_src_outside()
            return files

        with mock.patch.object(review_runtime, "_verify_snapshot_files", read_then_link):
            self.assert_refused(
                f"Source snapshot contains a reparse-point directory: {self.root / 'src'}", contents=(True,)
            )


# validate_adapter_manifest, pinned the same way, for an entrypoint manifest (schema 1) and a specialists manifest
# (schema 2). The specialists' own fields belong to _validate_specialists; one of its errors shows where it runs.
ManifestMutation = Callable[[Any], Any]
Key = str | int
MANIFEST_FIELDS = "Adapter manifest fields do not match the protocol"
UNSUPPORTED = "Adapter manifest protocol version is unsupported"
BAD_ID = "Adapter manifest id is invalid"
BAD_SUPPORTS = "Adapter supports must contain unique supported modes"
BAD_CAPABILITIES = "Adapter required_capabilities is invalid"
TWICE = "Adapter declares a file more than once"
RESERVED = "Adapter declares a reserved path"


def _entrypoint_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "team-review",
        "protocol_version": 1,
        "supports": ["initial", "re-review"],
        "required_capabilities": ["read-diff"],
        "entrypoint": "SKILL.md",
        "resources": ["references/guide.md", "./references/style.md"],
        "agent_profiles": ["agents/checker.md"],
    }


def _specialists_manifest() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "id": "team-specialists",
        "protocol_version": 1,
        "kind": "specialists",
        "supports": ["initial"],
        "required_capabilities": ["agent-delegation", "read-diff"],
        "resources": ["shared/guide.md"],
        "specialists": [
            {
                "id": "python-reviewer",
                "category": " Python ",
                "profile": "agents/python.md",
                "include": [r"\.py$"],
                "exclude": [],
                "resources": ["shared/python.md"],
                "when": "window",
            }
        ],
        "conditions": {"window": {"script": "conditions/window.py"}},
    }


def _put(path: tuple[Key, ...], value: Any) -> ManifestMutation:
    def apply(manifest: Any) -> Any:
        target = manifest
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = copy.deepcopy(value)
        return manifest

    return apply


def _drop(key: str) -> ManifestMutation:
    def apply(manifest: Any) -> Any:
        del manifest[key]
        return manifest

    return apply


def _whole(value: Any) -> ManifestMutation:
    return lambda _ignored: copy.deepcopy(value)


def _both(*mutations: ManifestMutation) -> ManifestMutation:
    def apply(manifest: Any) -> Any:
        for mutation in mutations:
            manifest = mutation(manifest)
        return manifest

    return apply


def _as_is(manifest: Any) -> Any:
    return manifest


def _entrypoint_result(**changes: Any) -> dict[str, Any]:
    """The base entrypoint manifest as validate_adapter_manifest returns it, with the given fields replaced."""
    result = _entrypoint_manifest()
    result["resources"] = ["references/guide.md", "references/style.md"]
    result.update(changes)
    return result


def _specialists_result(**changes: Any) -> dict[str, Any]:
    result = _specialists_manifest()
    result["specialists"][0]["category"] = "Python"
    result.update(changes)
    return result


# Manifests validate_adapter_manifest accepts: (name, build, mutation, the dict it returns).
MANIFEST_ACCEPTED: list[tuple[str, Callable[[], dict[str, Any]], ManifestMutation, dict[str, Any]]] = [
    ("entrypoint", _entrypoint_manifest, _as_is, _entrypoint_result()),
    (
        "entrypoint paths are normalized",
        _entrypoint_manifest,
        _both(_put(("entrypoint",), "./SKILL.md"), _put(("agent_profiles",), ["agents//checker.md"])),
        _entrypoint_result(agent_profiles=["agents/checker.md"]),
    ),
    (
        "entrypoint with no resources, profiles, or capabilities",
        _entrypoint_manifest,
        _both(_put(("resources",), []), _put(("agent_profiles",), []), _put(("required_capabilities",), [])),
        _entrypoint_result(resources=[], agent_profiles=[], required_capabilities=[]),
    ),
    (
        "protocol version true, which equals 1",
        _entrypoint_manifest,
        _put(("protocol_version",), True),
        _entrypoint_result(protocol_version=True),
    ),
    (
        "an id of 64 characters",
        _entrypoint_manifest,
        _put(("id",), "a" + "-" * 63),
        _entrypoint_result(id="a" + "-" * 63),
    ),
    (
        "re-review only",
        _entrypoint_manifest,
        _put(("supports",), ["re-review"]),
        _entrypoint_result(supports=["re-review"]),
    ),
    (
        "a materialization.json below the root, where the record is not written",
        _entrypoint_manifest,
        _put(("resources",), ["references/materialization.json"]),
        _entrypoint_result(resources=["references/materialization.json"]),
    ),
    ("specialists", _specialists_manifest, _as_is, _specialists_result()),
    (
        # agent-delegation is a dispatch requirement, negotiated when a review starts, not a manifest rule.
        "specialists without agent-delegation, which may also run inline",
        _specialists_manifest,
        _put(("required_capabilities",), ["read-diff"]),
        _specialists_result(required_capabilities=["read-diff"]),
    ),
    (
        "specialists that review uncovered files",
        _specialists_manifest,
        _put(("uncovered",), "review"),
        _specialists_result(uncovered="review"),
    ),
    (
        "specialists that ignore uncovered files",
        _specialists_manifest,
        _put(("uncovered",), "ignore"),
        _specialists_result(uncovered="ignore"),
    ),
    (
        "specialists whose findings name issue-type categories",
        _specialists_manifest,
        _put(("finding_categories",), ["Correctness", "Test Coverage"]),
        _specialists_result(finding_categories=["Correctness", "Test Coverage"]),
    ),
    (
        "specialists with a category for findings no other fits",
        _specialists_manifest,
        lambda manifest: {
            **manifest,
            "finding_categories": ["Correctness", "Other"],
            "fallback_finding_category": "Other",
        },
        _specialists_result(finding_categories=["Correctness", "Other"], fallback_finding_category="Other"),
    ),
    (
        "specialists resources are normalized",
        _specialists_manifest,
        _put(("resources",), ["./shared/guide.md"]),
        _specialists_result(),
    ),
    (
        "a profile that is also a resource",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "shared/guide.md"),
        _specialists_result(
            specialists=[{**_specialists_result()["specialists"][0], "profile": "shared/guide.md"}],
        ),
    ),
]

# Manifests validate_adapter_manifest refuses: (name, build, mutation, error, message).
MANIFEST_REJECTED: list[tuple[str, Callable[[], dict[str, Any]], ManifestMutation, type[Exception], str]] = [
    ("a list", _entrypoint_manifest, _whole([]), RuntimeContractError, MANIFEST_FIELDS),
    ("null", _entrypoint_manifest, _whole(None), RuntimeContractError, MANIFEST_FIELDS),
    ("entrypoint without an id", _entrypoint_manifest, _drop("id"), RuntimeContractError, MANIFEST_FIELDS),
    (
        "entrypoint with an extra field",
        _entrypoint_manifest,
        _put(("extra",), 1),
        RuntimeContractError,
        MANIFEST_FIELDS,
    ),
    (
        "entrypoint with uncovered",
        _entrypoint_manifest,
        _put(("uncovered",), "review"),
        RuntimeContractError,
        MANIFEST_FIELDS,
    ),
    (
        "entrypoint with a kind",
        _entrypoint_manifest,
        _put(("kind",), "entrypoint"),
        RuntimeContractError,
        MANIFEST_FIELDS,
    ),
    ("specialists without a kind", _specialists_manifest, _drop("kind"), RuntimeContractError, MANIFEST_FIELDS),
    (
        "specialists with an entrypoint",
        _specialists_manifest,
        _put(("entrypoint",), "SKILL.md"),
        RuntimeContractError,
        MANIFEST_FIELDS,
    ),
    (
        "specialists fields under schema 1",
        _specialists_manifest,
        _put(("schema_version",), 1),
        RuntimeContractError,
        MANIFEST_FIELDS,
    ),
    ("schema 3", _entrypoint_manifest, _put(("schema_version",), 3), RuntimeContractError, UNSUPPORTED),
    ("schema as a string", _entrypoint_manifest, _put(("schema_version",), "1"), RuntimeContractError, UNSUPPORTED),
    ("protocol 2", _entrypoint_manifest, _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    (
        "specialists protocol 2",
        _specialists_manifest,
        _put(("protocol_version",), 2),
        RuntimeContractError,
        UNSUPPORTED,
    ),
    ("an id that is not a string", _entrypoint_manifest, _put(("id",), 5), RuntimeContractError, BAD_ID),
    ("an empty id", _entrypoint_manifest, _put(("id",), ""), RuntimeContractError, BAD_ID),
    ("an uppercase id", _entrypoint_manifest, _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    ("an id starting with a dash", _entrypoint_manifest, _put(("id",), "-team"), RuntimeContractError, BAD_ID),
    ("an id of 65 characters", _entrypoint_manifest, _put(("id",), "a" * 65), RuntimeContractError, BAD_ID),
    (
        "supports that is a string",
        _entrypoint_manifest,
        _put(("supports",), "initial"),
        RuntimeContractError,
        BAD_SUPPORTS,
    ),
    ("empty supports", _entrypoint_manifest, _put(("supports",), []), RuntimeContractError, BAD_SUPPORTS),
    (
        "an unknown mode",
        _entrypoint_manifest,
        _put(("supports",), ["initial", "full"]),
        RuntimeContractError,
        BAD_SUPPORTS,
    ),
    (
        "a mode that is not a string",
        _entrypoint_manifest,
        _put(("supports",), [[]]),
        RuntimeContractError,
        BAD_SUPPORTS,
    ),
    (
        "a mode listed twice",
        _entrypoint_manifest,
        _put(("supports",), ["initial", "initial"]),
        RuntimeContractError,
        BAD_SUPPORTS,
    ),
    (
        "capabilities that are a string",
        _entrypoint_manifest,
        _put(("required_capabilities",), "read-diff"),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    (
        "a capability listed twice",
        _entrypoint_manifest,
        _put(("required_capabilities",), ["read-diff", "read-diff"]),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    (
        "a capability that is not a string",
        _entrypoint_manifest,
        _put(("required_capabilities",), [5]),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    (
        "an empty capability",
        _entrypoint_manifest,
        _put(("required_capabilities",), [""]),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    (
        "an entrypoint that is not a string",
        _entrypoint_manifest,
        _put(("entrypoint",), None),
        RuntimeContractError,
        "entrypoint must be a non-empty POSIX relative path",
    ),
    (
        "an entrypoint that escapes",
        _entrypoint_manifest,
        _put(("entrypoint",), "../SKILL.md"),
        RuntimeContractError,
        "entrypoint is unsafe: '../SKILL.md'",
    ),
    (
        "resources that are not an array",
        _entrypoint_manifest,
        _put(("resources",), "references/guide.md"),
        RuntimeContractError,
        "Adapter resources and agent_profiles must be arrays",
    ),
    (
        "profiles that are not an array",
        _entrypoint_manifest,
        _put(("agent_profiles",), {}),
        RuntimeContractError,
        "Adapter resources and agent_profiles must be arrays",
    ),
    (
        "an unsafe resource",
        _entrypoint_manifest,
        _put(("resources", 1), "C:/x.md"),
        RuntimeContractError,
        "resources[1] is unsafe: 'C:/x.md'",
    ),
    (
        "a backslash profile",
        _entrypoint_manifest,
        _put(("agent_profiles", 0), "agents\\checker.md"),
        RuntimeContractError,
        "agent_profiles[0] must be a non-empty POSIX relative path",
    ),
    (
        "a resource that is the entrypoint",
        _entrypoint_manifest,
        _put(("resources", 0), "./SKILL.md"),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a profile that is a resource",
        _entrypoint_manifest,
        _put(("agent_profiles", 0), "references/style.md"),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a reserved entrypoint, once normalized",
        _entrypoint_manifest,
        _put(("entrypoint",), "./materialization.json"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved entrypoint resource",
        _entrypoint_manifest,
        _put(("resources",), ["materialization.json"]),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved agent profile",
        _entrypoint_manifest,
        _put(("agent_profiles", 0), "materialization.json"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved entrypoint resource in another case",
        _entrypoint_manifest,
        _put(("resources",), ["Materialization.json"]),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved entrypoint in upper case",
        _entrypoint_manifest,
        _put(("entrypoint",), "MATERIALIZATION.JSON"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "another kind",
        _specialists_manifest,
        _put(("kind",), "entrypoint"),
        RuntimeContractError,
        "Adapter manifest kind is unsupported",
    ),
    (
        "an unknown uncovered policy",
        _specialists_manifest,
        _put(("uncovered",), "skip"),
        RuntimeContractError,
        "Adapter uncovered must be review or ignore",
    ),
    (
        "a null uncovered policy",
        _specialists_manifest,
        _put(("uncovered",), None),
        RuntimeContractError,
        "Adapter uncovered must be review or ignore",
    ),
    *(
        (
            f"finding categories that are {name}",
            _specialists_manifest,
            _put(("finding_categories",), categories),
            RuntimeContractError,
            "Adapter finding_categories must be a non-empty list of distinct one-line names of at most 60 characters, "
            "without backticks, quotes, or pipes",
        )
        for name, categories in (
            ("empty", []),
            ("not a list", "Style"),
            ("repeated without regard to case", ["Style", "style"]),
            ("not text", ["Style", 3]),
            ("blank", [" "]),
            ("padded", [" Style"]),
            ("on two lines", ["Style\nRisk"]),
            ("quoted", ['Style "nits"']),
            ("too long", ["x" * 61]),
        )
    ),
    (
        "a fallback category without finding categories",
        _specialists_manifest,
        _put(("fallback_finding_category",), "Other"),
        RuntimeContractError,
        "Adapter fallback_finding_category must be one of its finding_categories",
    ),
    (
        "a fallback category that is not one of them",
        _specialists_manifest,
        lambda manifest: {**manifest, "finding_categories": ["Correctness"], "fallback_finding_category": "Other"},
        RuntimeContractError,
        "Adapter fallback_finding_category must be one of its finding_categories",
    ),
    (
        "specialists resources that are not an array",
        _specialists_manifest,
        _put(("resources",), "shared/guide.md"),
        RuntimeContractError,
        "resources must be an array",
    ),
    (
        "an unsafe specialists resource",
        _specialists_manifest,
        _put(("resources", 0), "/shared/guide.md"),
        RuntimeContractError,
        "resources[0] is unsafe: '/shared/guide.md'",
    ),
    (
        "conditions that are not an object",
        _specialists_manifest,
        _put(("conditions",), []),
        RuntimeContractError,
        "Adapter conditions must be an object",
    ),
    (
        "a resource listed twice",
        _specialists_manifest,
        _put(("resources",), ["shared/guide.md", "./shared/guide.md"]),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a reserved resource",
        _specialists_manifest,
        _put(("resources",), ["materialization.json"]),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved profile",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "materialization.json"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved specialist resource",
        _specialists_manifest,
        _put(("specialists", 0, "resources"), ["materialization.json"]),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved condition script",
        _specialists_manifest,
        _put(("conditions", "window", "script"), "materialization.json"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved resource in another case",
        _specialists_manifest,
        _put(("resources",), ["Materialization.json"]),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a reserved profile in upper case",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "MATERIALIZATION.JSON"),
        RuntimeContractError,
        RESERVED,
    ),
]

ManifestStage = tuple[str, ManifestMutation, type[Exception], str]
# One fault per check of each kind of manifest, in the order validate_adapter_manifest detects them.
ENTRYPOINT_STAGES: list[ManifestStage] = [
    ("shape", _put(("extra",), 1), RuntimeContractError, MANIFEST_FIELDS),
    ("protocol", _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    ("id", _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    ("supports", _put(("supports",), []), RuntimeContractError, BAD_SUPPORTS),
    ("capabilities", _put(("required_capabilities",), [""]), RuntimeContractError, BAD_CAPABILITIES),
    ("entrypoint", _put(("entrypoint",), "../SKILL.md"), RuntimeContractError, "entrypoint is unsafe: '../SKILL.md'"),
    (
        "arrays",
        _put(("agent_profiles",), {}),
        RuntimeContractError,
        "Adapter resources and agent_profiles must be arrays",
    ),
    ("resources", _put(("resources", 0), "../x.md"), RuntimeContractError, "resources[0] is unsafe: '../x.md'"),
    (
        "profiles",
        _put(("agent_profiles", 0), "../x.md"),
        RuntimeContractError,
        "agent_profiles[0] is unsafe: '../x.md'",
    ),
    ("duplicates", _put(("agent_profiles", 0), "SKILL.md"), RuntimeContractError, TWICE),
    ("reserved", _put(("resources", 1), "materialization.json"), RuntimeContractError, RESERVED),
]
SPECIALISTS_STAGES: list[ManifestStage] = [
    ("shape", _put(("extra",), 1), RuntimeContractError, MANIFEST_FIELDS),
    ("protocol", _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    ("id", _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    ("supports", _put(("supports",), []), RuntimeContractError, BAD_SUPPORTS),
    (
        "capabilities",
        _put(("required_capabilities",), ["read-diff", "read-diff"]),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    ("kind", _put(("kind",), "entrypoint"), RuntimeContractError, "Adapter manifest kind is unsupported"),
    ("uncovered", _put(("uncovered",), "skip"), RuntimeContractError, "Adapter uncovered must be review or ignore"),
    ("resources", _put(("resources",), "x"), RuntimeContractError, "resources must be an array"),
    ("specialists", _put(("conditions",), []), RuntimeContractError, "Adapter conditions must be an object"),
    ("duplicates", _put(("resources",), ["a.md", "a.md"]), RuntimeContractError, TWICE),
    ("reserved", _put(("specialists", 0, "profile"), "materialization.json"), RuntimeContractError, RESERVED),
]


class AdapterManifestValidationTests(unittest.TestCase):
    def assert_refused(self, manifest: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            validate_adapter_manifest(manifest)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_manifests_come_back_normalized_as_a_new_object(self) -> None:
        for name, build, mutation, expected in MANIFEST_ACCEPTED:
            with self.subTest(name):
                manifest = mutation(build())
                before = copy.deepcopy(manifest)
                result = validate_adapter_manifest(manifest)
                self.assertEqual(expected, result)
                self.assertEqual(list(expected), list(result))
                self.assertIsNot(manifest, result)
                self.assertEqual(before, manifest)

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, build, mutation, error, message in MANIFEST_REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(build()), error, message)

    def test_each_stage_is_refused_alone(self) -> None:
        for build, stages in ((_entrypoint_manifest, ENTRYPOINT_STAGES), (_specialists_manifest, SPECIALISTS_STAGES)):
            for name, mutation, error, message in stages:
                with self.subTest(f"{build.__name__}: {name}"):
                    self.assert_refused(mutation(build()), error, message)

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for build, stages in ((_entrypoint_manifest, ENTRYPOINT_STAGES), (_specialists_manifest, SPECIALISTS_STAGES)):
            for index, (name, _mutation, error, message) in enumerate(stages):
                with self.subTest(f"{build.__name__}: {name}"):
                    manifest = build()
                    for _later, mutation, _error, _message in reversed(stages[index:]):
                        manifest = mutation(manifest)
                    self.assert_refused(manifest, error, message)

    def test_a_capability_that_cannot_be_hashed_escapes_as_a_type_error(self) -> None:
        # The duplicate check builds a set before the item check runs. Python words the error differently across
        # versions, so only its class and the part every version shares are pinned.
        with self.assertRaises(TypeError) as caught:
            validate_adapter_manifest(_put(("required_capabilities",), [[]])(_entrypoint_manifest()))
        self.assertIs(TypeError, type(caught.exception))
        self.assertIn("unhashable type: 'list'", str(caught.exception))


class ReservedPathMaterializationTests(unittest.TestCase):
    """materialize_reviewer writes its own record to materialization.json, so a trusted commit that holds a reviewer
    file at that path, in any case (Windows file systems ignore it), must never reach it: the file would be written,
    then replaced by the record."""

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def _commit(self, checkout: Path, reserved: str) -> str:
        """A repository whose only commit holds SKILL.md and a reviewer file named reserved."""
        checkout.mkdir()
        self._git(checkout, "init", "-b", "main")
        (checkout / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
        (checkout / reserved).write_text('{"reviewer": "file"}\n', encoding="utf-8")
        self._git(checkout, "add", ".")
        self._git(
            checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "both"
        )
        return self._git(checkout, "rev-parse", "HEAD")

    def test_a_reserved_path_in_either_schema_materializes_nothing(self) -> None:
        specialists = {
            "schema_version": 2,
            "id": "team-specialists",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation", "read-diff"],
            "specialists": [
                {
                    "id": "python-reviewer",
                    "category": "Python",
                    "profile": "SKILL.md",
                    "include": [r"\.py$"],
                    "exclude": [],
                    "resources": [],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        # (name, the reviewer file's name, the manifest that declares it)
        cases: list[tuple[str, str, dict[str, Any]]] = [
            (
                "entrypoint",
                "materialization.json",
                {**_entrypoint_manifest(), "entrypoint": "materialization.json", "resources": [], "agent_profiles": []},
            ),
            (
                "resource",
                "materialization.json",
                {**_entrypoint_manifest(), "resources": ["materialization.json"], "agent_profiles": []},
            ),
            (
                "agent profile",
                "materialization.json",
                {**_entrypoint_manifest(), "resources": [], "agent_profiles": ["materialization.json"]},
            ),
            (
                "resource in another case",
                "Materialization.json",
                {**_entrypoint_manifest(), "resources": ["Materialization.json"], "agent_profiles": []},
            ),
            (
                "specialists resource in another case",
                "MATERIALIZATION.JSON",
                {**specialists, "resources": ["MATERIALIZATION.JSON"]},
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (name, reserved, manifest) in enumerate(cases):
                with self.subTest(name):
                    commit = self._commit(root / f"checkout-{index}", reserved)
                    destination = root / f"destination-{index}"
                    with self.assertRaises(RuntimeContractError) as caught:
                        review_runtime.materialize_reviewer(root / f"checkout-{index}", commit, manifest, destination)
                    self.assertEqual(RESERVED, str(caught.exception))
                    self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

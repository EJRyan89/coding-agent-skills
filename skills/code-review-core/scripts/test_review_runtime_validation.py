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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_runtime
from git_test_base import GitTestCase
from review_config import ConfigurationError
from review_runtime import (
    RuntimeContractError,
    RuntimeContractFieldError,
    validate_adapter_manifest,
    verify_source_snapshot,
)

HEAD = "a" * 40
APP = b"print('app')\n"
README = b"# Readme\n"
# A message is literal, or built from the snapshot root for the errors that name a path under it.
Message = str | Callable[[Path], str]
# A field of the wrong kind, refused in the typed-field reader's one shape.
F = RuntimeContractFieldError
SNAPSHOT = "source-snapshot.json"
# What a field error names as found, for each wrong value the cases below use, written out independently of the reader.
FOUND: dict[str, str] = {
    "None": "null",
    "True": "boolean true",
    "False": "boolean false",
    "1.0": "number 1.0",
    "2.0": "number 2.0",
    "[]": "an array",
    "{}": "an object",
}
# The kinds a source snapshot manifest's fields are read as, written out independently of the reader.
HEX64 = "a lowercase hexadecimal digest of 64 characters"
COMMIT_KIND = "a lowercase hexadecimal digest of 40 or 64 characters"
NOT_HEX = "a string with a character that is not a lowercase hexadecimal digit"
REASON_KIND = (
    'one of "agent-instruction", "binary", "configured", "file-size-limit", "non-regular", "symbolic-link", '
    '"unextractable", "unsafe-path"'
)


def _found(value: Any, *, text: bool = False) -> str:
    """What a field error names as found: a listed literal, an integer, a quoted short string, or an array or object;
    where a string is expected (`text`), an empty or blank string is named as such."""
    if text and isinstance(value, str) and not value.strip():
        return "a blank string" if value else "an empty string"
    if repr(value) in FOUND:
        return FOUND[repr(value)]
    if isinstance(value, int):
        return f"integer {value}"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return f"string {json.dumps(value)}"


def _wrong(path: str, expected: str, value: Any) -> str:
    """The one shape a field of the wrong kind is refused with, written out independently of the reader."""
    return f"{path} must be {expected}, found {_found(value, text='string' in expected)}"


def _unknown(path: str, field: str) -> str:
    return f'{path} must be an object of known fields, found unknown field "{field}"'


def _missing(path: str, *fields: str) -> str:
    noun, pronoun = ("fields", "them") if len(fields) > 1 else ("field", "it")
    return f"{path} must be an object with {noun} {', '.join(fields)}, found an object without {pronoun}"


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


# Snapshots verify_source_snapshot accepts, from the base snapshot.
ACCEPTED: list[tuple[str, Mutation]] = [
    ("base", _same),
    ("a manifest with a byte-order mark", _attribute("bom", True)),
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
    *(
        (
            f"schema version {value!r}",
            _meta("schema_version", value),
            F,
            _wrong(f"{SNAPSHOT}.schema_version", "an integer", value),
        )
        for value in (True, 1.0)
    ),
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
    ("metadata that is a list", _attribute("metadata", []), F, _wrong(SNAPSHOT, "an object", [])),
    ("metadata that is a string", _attribute("metadata", "x"), F, _wrong(SNAPSHOT, "an object", "x")),
    *(
        (f"metadata without {field}", _without(field), F, _missing(SNAPSHOT, field))
        for field in ("schema_version", "repository", "source_commit", "source_hashes", "excluded_paths")
    ),
    ("metadata with an extra field", _meta("extra", None), F, _unknown(SNAPSHOT, "extra")),
    (
        "schema version 2",
        _meta("schema_version", 2),
        RuntimeContractError,
        "Source snapshot schema version is unsupported",
    ),
    (
        "schema version as a string",
        _meta("schema_version", "1"),
        F,
        _wrong(f"{SNAPSHOT}.schema_version", "an integer", "1"),
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
        F,
        "source-snapshot.json.repository must be a non-empty string, found integer 5",
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
        F,
        f"{SNAPSHOT}.source_commit must be {COMMIT_KIND}, found a string of 4 characters",
    ),
    (
        "an uppercase commit",
        _chain(_meta("source_commit", "A" * 40), _attribute("commit", "A" * 40)),
        F,
        f"{SNAPSHOT}.source_commit must be {COMMIT_KIND}, found {NOT_HEX}",
    ),
    (
        "a commit that is not a string",
        _meta("source_commit", 5),
        F,
        _wrong(f"{SNAPSHOT}.source_commit", COMMIT_KIND, 5),
    ),
    ("hashes that are a list", _meta("source_hashes", []), F, _wrong(f"{SNAPSHOT}.source_hashes", "an object", [])),
    (
        "exclusions that are a list",
        _meta("excluded_paths", []),
        F,
        _wrong(f"{SNAPSHOT}.excluded_paths", "an object", []),
    ),
    (
        "more files than the limit",
        _limit("MAX_SOURCE_SNAPSHOT_FILES", 1),
        RuntimeContractError,
        "Source snapshot exceeds the file-count limit",
    ),
    (
        "an empty path",
        _rename_first("source_hashes", ""),
        F,
        "a key of source-snapshot.json.source_hashes must be a non-empty string, found an empty string",
    ),
    (
        "a backslash path",
        _rename_first("source_hashes", "src\\app.py"),
        RuntimeContractError,
        "a key of source-snapshot.json.source_hashes must be a non-empty POSIX relative path",
    ),
    (
        "a path that escapes",
        _rename_first("source_hashes", "../app.py"),
        RuntimeContractError,
        "a key of source-snapshot.json.source_hashes is unsafe: '../app.py'",
    ),
    (
        "an absolute path",
        _rename_first("source_hashes", "/src/app.py"),
        RuntimeContractError,
        "a key of source-snapshot.json.source_hashes is unsafe: '/src/app.py'",
    ),
    (
        "a path with a Windows-unsafe character",
        _rename_first("source_hashes", "src/a:pp.py"),
        RuntimeContractError,
        "a key of source-snapshot.json.source_hashes is unsafe: 'src/a:pp.py'",
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
        F,
        _wrong(f"{SNAPSHOT}.source_hashes.src/app.py", HEX64, 5),
    ),
    (
        "an uppercase hash",
        _set_first("source_hashes", "A" * 64),
        F,
        f"{SNAPSHOT}.source_hashes.src/app.py must be {HEX64}, found {NOT_HEX}",
    ),
    (
        "a short hash",
        _set_first("source_hashes", "a" * 40),
        F,
        f"{SNAPSHOT}.source_hashes.src/app.py must be {HEX64}, found a string of 40 characters",
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
        F,
        f"{SNAPSHOT}.source_hashes.src/app.py must be {HEX64}, found a string of 1 character",
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
        F,
        "a key of source-snapshot.json.excluded_paths must be a non-empty string, found an empty string",
    ),
    (
        "an exclusion that escapes",
        _entry("excluded_paths", "../x.bin", "binary"),
        RuntimeContractError,
        "a key of source-snapshot.json.excluded_paths is unsafe: '../x.bin'",
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
        F,
        _wrong(f"{SNAPSHOT}.excluded_paths.x.bin", REASON_KIND, 5),
    ),
    (
        "an unknown exclusion reason",
        _entry("excluded_paths", "x.bin", "too-big"),
        F,
        _wrong(f"{SNAPSHOT}.excluded_paths.x.bin", REASON_KIND, "too-big"),
    ),
    (
        "an exclusion reason in another case",
        _entry("excluded_paths", "x.bin", "Binary"),
        F,
        _wrong(f"{SNAPSHOT}.excluded_paths.x.bin", REASON_KIND, "Binary"),
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
    ("metadata fields", _meta("extra", None), F, _unknown(SNAPSHOT, "extra")),
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
    ("maps", _meta("excluded_paths", []), F, _wrong(f"{SNAPSHOT}.excluded_paths", "an object", [])),
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
        "a key of source-snapshot.json.source_hashes is unsafe: '../app.py'",
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
        F,
        f"{SNAPSHOT}.source_hashes.src/app.py must be {HEX64}, found a string of 1 character",
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
        "a key of source-snapshot.json.excluded_paths is unsafe: '../x.bin'",
    ),
    (
        "exclusion reason",
        _set_first("excluded_paths", "too-big"),
        F,
        _wrong(f"{SNAPSHOT}.excluded_paths.assets/logo.png", REASON_KIND, "too-big"),
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

# A wrong-typed value for each kind of field verify_source_snapshot reads, in the reader's one shape: an object, an
# integer, a string, a hash, an enumerated value, and each optional map.
SNAPSHOT_KINDS: list[tuple[str, Mutation, str]] = [
    ("manifest", _attribute("metadata", None), _wrong(SNAPSHOT, "an object", None)),
    ("schema_version", _meta("schema_version", [1]), _wrong(f"{SNAPSHOT}.schema_version", "an integer", [])),
    ("repository", _meta("repository", ""), _wrong(f"{SNAPSHOT}.repository", "a non-empty string", "")),
    ("repository null", _meta("repository", None), _wrong(f"{SNAPSHOT}.repository", "a non-empty string", None)),
    ("source_commit", _meta("source_commit", {}), _wrong(f"{SNAPSHOT}.source_commit", COMMIT_KIND, {})),
    ("source_hashes", _meta("source_hashes", "x"), _wrong(f"{SNAPSHOT}.source_hashes", "an object", "x")),
    ("a source hash", _set_first("source_hashes", None), _wrong(f"{SNAPSHOT}.source_hashes.src/app.py", HEX64, None)),
    ("excluded_paths", _meta("excluded_paths", 1), _wrong(f"{SNAPSHOT}.excluded_paths", "an object", 1)),
    (
        "an exclusion reason",
        _set_first("excluded_paths", ["binary"]),
        _wrong(f"{SNAPSHOT}.excluded_paths.assets/logo.png", REASON_KIND, ["binary"]),
    ),
    ("fetchable", _meta("fetchable", []), _wrong(f"{SNAPSHOT}.fetchable", "an object", [])),
    (
        "a fetchable blob",
        _meta("fetchable", {"src/other.py": 5}),
        _wrong(f"{SNAPSHOT}.fetchable.src/other.py", "a lowercase hexadecimal digest of 40 characters", 5),
    ),
    (
        "a fetchable path",
        _meta("fetchable", {"": "b" * 40}),
        "a key of source-snapshot.json.fetchable must be a non-empty string, found an empty string",
    ),
    ("extracted", _meta("extracted", []), _wrong(f"{SNAPSHOT}.extracted", "an object", [])),
    (
        "an extracted form",
        _meta("extracted", {"doc.docx": True}),
        _wrong(f"{SNAPSHOT}.extracted.doc.docx", 'one of "docx", "pdf"', True),
    ),
]


class SourceSnapshotVerificationTests(GitTestCase):
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
        snapshot = _meta("repository", "Owner/Repo")(_snapshot())
        result = self.verify(snapshot, self.materialize(snapshot))
        self.assertEqual("Owner/Repo", result["repository"])
        self.assertIs(int, type(result["schema_version"]))
        self.assertEqual(1, result["schema_version"])
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

    def test_each_kind_of_field_refuses_a_wrong_type(self) -> None:
        for name, mutation, message in SNAPSHOT_KINDS:
            with self.subTest(name):
                self.assert_refused(mutation(_snapshot()), F, message)

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
            excluded(_snapshot()), F, _wrong(f"{SNAPSHOT}.excluded_paths.first.bin", REASON_KIND, "too-big")
        )


def directory_link(link: Path, target: Path) -> None:
    """A link at `link` to the directory `target` that any user can make: on Windows a directory junction, a reparse
    point no privilege guards where a symbolic link needs one, and elsewhere a symbolic link."""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


class SnapshotLinkTests(GitTestCase):
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
        directory_link(self.root / "src", self.outside)
        self.assertEqual(APP, (self.root / "src" / "app.py").read_bytes())

    def test_the_snapshot_without_links_verifies(self) -> None:
        for contents in (True, False):
            self.assertEqual(_snapshot().metadata, self.verify(contents))

    def test_a_junction_folder_made_below_the_root_is_refused_and_not_entered(self) -> None:
        directory_link(self.root / "linked", self.outside)
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
        directory_link(self.root / "linked", self.outside)
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


UTIL = b"def util():\n    return 1\n"
UTIL_BLOB = hashlib.sha1(b"blob %d\0" % len(UTIL) + UTIL, usedforsecurity=False).hexdigest()
MINUTE = 60 * 10**9  # in the nanoseconds a file's time is set in


class SnapshotStampTests(GitTestCase):
    """stamp_source_snapshot and verify_stamped_snapshot, the Copilot CLI host's check before it starts a reviewer: a
    snapshot whose listing still shows every file prepare wrote at the size and time prepare saw re-reads only the files
    that time cannot vouch for, and anything else gets the full contents pass. Times are set explicitly, so whether a
    file counts as written before the manifest never depends on the clock's resolution."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-stamp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "source"
        self.metadata = copy.deepcopy(_snapshot().metadata)
        self.write(_snapshot().files)

    def write(self, files: dict[str, bytes]) -> None:
        """Write the files, then the manifest, and date every file a minute before the manifest."""
        for relative, content in files.items():
            self.put(relative, content)
        self.manifest().write_text(json.dumps(self.metadata), encoding="utf-8")
        for relative in files:
            self.age(relative, self.manifest_time() - MINUTE)

    def manifest(self) -> Path:
        return self.root / "source-snapshot.json"

    def manifest_time(self) -> int:
        return self.manifest().stat().st_mtime_ns

    def put(self, relative: str, content: bytes, modified: int | None = None) -> None:
        target = self.root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if modified is not None:
            self.age(relative, modified)

    def age(self, relative: str, modified: int) -> None:
        os.utime(self.root.joinpath(*relative.split("/")), ns=(modified, modified))

    def stamp(self) -> str:
        return review_runtime.stamp_source_snapshot(self.root, expected_repository="owner/repo", expected_commit=HEAD)

    def verify(self, stamp: str | None) -> tuple[dict[str, Any], list[str]]:
        """The metadata the host's check returns, and the snapshot files it read, the manifest left out, which
        `self.read` also holds when it raises."""
        self.read: list[str] = []
        read = self.read
        original = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            if path != self.manifest():
                read.append(path.relative_to(self.root).as_posix())
            return original(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            metadata = review_runtime.verify_stamped_snapshot(
                self.root, expected_repository="owner/repo", expected_commit=HEAD, stamp=stamp
            )
        return metadata, read

    def assert_refused(self, stamp: str | None, message: str) -> None:
        with self.assertRaises(RuntimeContractError) as caught:
            self.verify(stamp)
        self.assertEqual(message, str(caught.exception))

    def test_a_stamp_is_one_digest_and_names_the_snapshot_it_was_taken_of(self) -> None:
        stamp = self.stamp()
        self.assertRegex(stamp, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(stamp, self.stamp())
        self.age("README.md", self.manifest_time() - 2 * MINUTE)
        self.assertNotEqual(stamp, self.stamp(), "a file's time is part of the stamp")

    def test_a_stamp_is_taken_only_of_a_snapshot_that_verifies(self) -> None:
        self.put("extra.py", b"x = 1\n")
        with self.assertRaises(RuntimeContractError) as caught:
            self.stamp()
        self.assertEqual("Source snapshot file set mismatch; missing=[], extra=['extra.py']", str(caught.exception))

    def test_a_matching_stamp_reads_no_file_written_before_the_manifest(self) -> None:
        self.assertEqual((self.metadata, []), self.verify(self.stamp()))

    def test_a_file_written_in_the_manifests_clock_tick_is_read_though_the_stamp_matches(self) -> None:
        # A rewrite in the same tick as prepare's write would keep its time, so time vouches only for older files.
        self.age("README.md", self.manifest_time())
        stamp = self.stamp()
        self.assertEqual((self.metadata, ["README.md"]), self.verify(stamp))
        self.put("README.md", b"# Readm!\n", self.manifest_time())
        self.assert_refused(stamp, "Source snapshot hash mismatch: README.md")

    def test_a_file_changed_after_the_stamp_is_refused_by_the_full_pass(self) -> None:
        stamp = self.stamp()
        self.put("src/app.py", b"print('pwn')\n")  # the same size, at the time it was written
        self.assertEqual(len(APP), len(b"print('pwn')\n"))
        self.assert_refused(stamp, "Source snapshot hash mismatch: src/app.py")

    def test_a_file_added_after_the_stamp_is_refused_before_anything_is_read(self) -> None:
        stamp = self.stamp()
        self.put("src/added.py", b"x = 1\n")
        self.assert_refused(stamp, "Source snapshot file set mismatch; missing=[], extra=['src/added.py']")
        self.assertEqual([], self.read)

    def test_a_file_removed_after_the_stamp_is_refused(self) -> None:
        stamp = self.stamp()
        (self.root / "README.md").unlink()
        self.assert_refused(stamp, "Source snapshot file is missing: README.md")

    def test_a_manifest_rewritten_after_the_stamp_is_refused_by_the_full_pass(self) -> None:
        stamp = self.stamp()
        modified = self.manifest_time()
        self.metadata["source_hashes"]["README.md"] = "0" * 64
        self.manifest().write_text(json.dumps(self.metadata), encoding="utf-8")
        os.utime(self.manifest(), ns=(modified, modified))  # its time kept: its bytes are in the stamp
        self.assert_refused(stamp, "Source snapshot hash mismatch: README.md")

    def test_a_file_touched_but_unchanged_passes_the_full_pass(self) -> None:
        stamp = self.stamp()
        self.age("README.md", self.manifest_time() + MINUTE)
        self.assertEqual((self.metadata, ["src/app.py", "README.md"]), self.verify(stamp))

    def test_without_a_stamp_or_with_another_snapshots_every_file_is_read(self) -> None:
        for stamp in (None, "0" * 64, "not a digest"):
            with self.subTest(stamp=stamp):
                self.assertEqual((self.metadata, ["src/app.py", "README.md"]), self.verify(stamp))

    def test_a_lazy_snapshots_fetched_files_are_read_whenever_they_are_held(self) -> None:
        metadata = {**copy.deepcopy(self.metadata), "fetchable": {"lib/util.py": UTIL_BLOB}}
        self.metadata = metadata
        self.write({})
        stamp = self.stamp()
        self.assertEqual((metadata, []), self.verify(stamp))
        self.put("lib/util.py", UTIL, self.manifest_time() - MINUTE)  # fetched after the stamp
        self.assertEqual((metadata, ["lib/util.py"]), self.verify(stamp))
        self.put("lib/util.py", UTIL.replace(b"1", b"2"), self.manifest_time() - MINUTE)
        self.assert_refused(stamp, "Source snapshot file does not match its blob: lib/util.py")


# validate_adapter_manifest, pinned the same way, for an entrypoint manifest (schema 1) and a specialists manifest
# (schema 2). The specialists' own fields belong to _validate_specialists; one of its errors shows where it runs.
ManifestMutation = Callable[[Any], Any]
Key = str | int
ENTRYPOINT = "entrypoint-manifest"
SPECIALISTS = "specialists-manifest"
SPECIALIST = f"{SPECIALISTS}.specialists[0]"
MODES_KIND = 'one of "initial", "re-review"'
MODEL_KIND = 'one of "fable", "haiku", "inherit", "opus", "sonnet" or null'
EFFORT_KIND = 'one of "high", "low", "max", "medium", "xhigh" or null'
CATEGORY_KIND = "a non-empty trimmed string of at most 60 characters"
READS_KIND = "a non-empty string of at most 200 characters"
GLOB_RULE = (
    "a repository-relative glob pattern without a backslash, a control character, or an empty, '.', or '..' segment"
)
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


def _second_specialist(**fields: Any) -> ManifestMutation:
    """Add a second specialist with files of its own, except the fields given."""

    def apply(manifest: Any) -> Any:
        manifest["specialists"].append(
            {
                "id": "docs-reviewer",
                "category": "Docs",
                "profile": "agents/docs.md",
                "include": [r"\.md$"],
                "exclude": [],
                "resources": ["shared/docs.md"],
                "when": None,
                **copy.deepcopy(fields),
            }
        )
        return manifest

    return apply


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
        # Read from the suite's references, never from the repository.
        "a profile the suite ships",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "suite:design-review"),
        _specialists_result(
            specialists=[{**_specialists_result()["specialists"][0], "profile": "suite:design-review"}],
        ),
    ),
    (
        # A suite profile is a name, not a repository file, so two specialists may name the same one.
        "two specialists that name the same suite profile",
        _specialists_manifest,
        _both(
            _put(("specialists", 0, "profile"), "suite:design-review"),
            _second_specialist(profile="suite:design-review"),
        ),
        _specialists_result(
            specialists=[
                {**_specialists_result()["specialists"][0], "profile": "suite:design-review"},
                {
                    "id": "docs-reviewer",
                    "category": "Docs",
                    "profile": "suite:design-review",
                    "include": [r"\.md$"],
                    "exclude": [],
                    "resources": ["shared/docs.md"],
                    "when": None,
                },
            ],
        ),
    ),
    (
        # Several specialists may share a profile or a guideline, which keeps its one role and its one source.
        "two specialists that share a profile and a guideline",
        _specialists_manifest,
        _second_specialist(profile="agents/python.md", resources=["shared/python.md"]),
        _specialists_result(
            specialists=[
                _specialists_result()["specialists"][0],
                {
                    "id": "docs-reviewer",
                    "category": "Docs",
                    "profile": "agents/python.md",
                    "include": [r"\.md$"],
                    "exclude": [],
                    "resources": ["shared/python.md"],
                    "when": None,
                },
            ],
        ),
    ),
    (
        "two conditions that share a script",
        _specialists_manifest,
        _put(("conditions", "open"), {"script": "conditions/window.py"}),
        _specialists_result(
            conditions={"window": {"script": "conditions/window.py"}, "open": {"script": "conditions/window.py"}}
        ),
    ),
    (
        "a condition that declares the snapshot paths it reads",
        _specialists_manifest,
        _put(("conditions", "window", "reads"), ["**/*.csproj", "global.json", "src/[ab]?/*.props"]),
        _specialists_result(
            conditions={
                "window": {
                    "script": "conditions/window.py",
                    "reads": ["**/*.csproj", "global.json", "src/[ab]?/*.props"],
                }
            }
        ),
    ),
    (
        # It reads only what the lazy snapshot holds anyway: the changed files and the analyzer settings.
        "a condition that declares it reads no other path",
        _specialists_manifest,
        _put(("conditions", "window", "reads"), []),
        _specialists_result(conditions={"window": {"script": "conditions/window.py", "reads": []}}),
    ),
]

# Manifests validate_adapter_manifest refuses: (name, build, mutation, error, message).
MANIFEST_REJECTED: list[tuple[str, Callable[[], dict[str, Any]], ManifestMutation, type[Exception], str]] = [
    *(
        (
            f"{build.__name__} {field} {value!r}",
            build,
            _put((field,), value),
            F,
            _wrong("manifest.schema_version" if field == "schema_version" else f"{root}.{field}", "an integer", value),
        )
        for build, root in ((_entrypoint_manifest, ENTRYPOINT), (_specialists_manifest, SPECIALISTS))
        for field, values in (
            ("schema_version", (True, 1.0) if build is _entrypoint_manifest else (True, 2.0)),
            ("protocol_version", (True, 1.0)),
        )
        for value in values
    ),
    ("a list", _entrypoint_manifest, _whole([]), F, _wrong("manifest", "an object", [])),
    ("null", _entrypoint_manifest, _whole(None), F, _wrong("manifest", "an object", None)),
    ("entrypoint without an id", _entrypoint_manifest, _drop("id"), F, _missing(ENTRYPOINT, "id")),
    ("entrypoint with an extra field", _entrypoint_manifest, _put(("extra",), 1), F, _unknown(ENTRYPOINT, "extra")),
    (
        "entrypoint with uncovered",
        _entrypoint_manifest,
        _put(("uncovered",), "review"),
        F,
        _unknown(ENTRYPOINT, "uncovered"),
    ),
    ("entrypoint with a kind", _entrypoint_manifest, _put(("kind",), "entrypoint"), F, _unknown(ENTRYPOINT, "kind")),
    ("specialists without a kind", _specialists_manifest, _drop("kind"), F, _missing(SPECIALISTS, "kind")),
    (
        "specialists with an entrypoint",
        _specialists_manifest,
        _put(("entrypoint",), "SKILL.md"),
        F,
        _unknown(SPECIALISTS, "entrypoint"),
    ),
    (
        "specialists fields under schema 1",
        _specialists_manifest,
        _put(("schema_version",), 1),
        F,
        _missing(ENTRYPOINT, "agent_profiles", "entrypoint"),
    ),
    ("schema 3", _entrypoint_manifest, _put(("schema_version",), 3), RuntimeContractError, UNSUPPORTED),
    (
        "schema as a string",
        _entrypoint_manifest,
        _put(("schema_version",), "1"),
        F,
        _wrong("manifest.schema_version", "an integer", "1"),
    ),
    ("protocol 2", _entrypoint_manifest, _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    (
        "specialists protocol 2",
        _specialists_manifest,
        _put(("protocol_version",), 2),
        RuntimeContractError,
        UNSUPPORTED,
    ),
    (
        "an id that is not a string",
        _entrypoint_manifest,
        _put(("id",), 5),
        F,
        _wrong(f"{ENTRYPOINT}.id", "a non-empty string", 5),
    ),
    ("an empty id", _entrypoint_manifest, _put(("id",), ""), F, _wrong(f"{ENTRYPOINT}.id", "a non-empty string", "")),
    ("an uppercase id", _entrypoint_manifest, _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    ("an id starting with a dash", _entrypoint_manifest, _put(("id",), "-team"), RuntimeContractError, BAD_ID),
    ("an id of 65 characters", _entrypoint_manifest, _put(("id",), "a" * 65), RuntimeContractError, BAD_ID),
    (
        "supports that is a string",
        _entrypoint_manifest,
        _put(("supports",), "initial"),
        F,
        _wrong(f"{ENTRYPOINT}.supports", "a non-empty array", "initial"),
    ),
    (
        "empty supports",
        _entrypoint_manifest,
        _put(("supports",), []),
        F,
        f"{ENTRYPOINT}.supports must be a non-empty array, found an empty array",
    ),
    (
        "an unknown mode",
        _entrypoint_manifest,
        _put(("supports",), ["initial", "full"]),
        F,
        _wrong(f"{ENTRYPOINT}.supports[1]", MODES_KIND, "full"),
    ),
    (
        "a mode that is not a string",
        _entrypoint_manifest,
        _put(("supports",), [[]]),
        F,
        _wrong(f"{ENTRYPOINT}.supports[0]", MODES_KIND, []),
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
        F,
        _wrong(f"{ENTRYPOINT}.required_capabilities", "an array", "read-diff"),
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
        F,
        _wrong(f"{ENTRYPOINT}.required_capabilities[0]", "a non-empty string", 5),
    ),
    (
        "an empty capability",
        _entrypoint_manifest,
        _put(("required_capabilities",), [""]),
        F,
        _wrong(f"{ENTRYPOINT}.required_capabilities[0]", "a non-empty string", ""),
    ),
    (
        "an entrypoint that is not a string",
        _entrypoint_manifest,
        _put(("entrypoint",), None),
        F,
        _wrong(f"{ENTRYPOINT}.entrypoint", "a non-empty string", None),
    ),
    (
        "an entrypoint that escapes",
        _entrypoint_manifest,
        _put(("entrypoint",), "../SKILL.md"),
        RuntimeContractError,
        "entrypoint-manifest.entrypoint is unsafe: '../SKILL.md'",
    ),
    (
        "resources that are not an array",
        _entrypoint_manifest,
        _put(("resources",), "references/guide.md"),
        F,
        _wrong(f"{ENTRYPOINT}.resources", "an array", "references/guide.md"),
    ),
    (
        "profiles that are not an array",
        _entrypoint_manifest,
        _put(("agent_profiles",), {}),
        F,
        _wrong(f"{ENTRYPOINT}.agent_profiles", "an array", {}),
    ),
    (
        "an unsafe resource",
        _entrypoint_manifest,
        _put(("resources", 1), "C:/x.md"),
        RuntimeContractError,
        "entrypoint-manifest.resources[1] is unsafe: 'C:/x.md'",
    ),
    (
        "a backslash profile",
        _entrypoint_manifest,
        _put(("agent_profiles", 0), "agents\\checker.md"),
        RuntimeContractError,
        "entrypoint-manifest.agent_profiles[0] must be a non-empty POSIX relative path",
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
        F,
        _wrong(f"{SPECIALISTS}.kind", 'one of "specialists"', "entrypoint"),
    ),
    (
        "an unknown uncovered policy",
        _specialists_manifest,
        _put(("uncovered",), "skip"),
        F,
        _wrong(f"{SPECIALISTS}.uncovered", 'one of "review", "ignore"', "skip"),
    ),
    (
        "a null uncovered policy",
        _specialists_manifest,
        _put(("uncovered",), None),
        F,
        _wrong(f"{SPECIALISTS}.uncovered", 'one of "review", "ignore"', None),
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
            ("repeated without regard to case", ["Style", "style"]),
            ("on two lines", ["Style\nRisk"]),
            ("quoted", ['Style "nits"']),
        )
    ),
    *(
        (f"finding categories that are {name}", _specialists_manifest, _put(("finding_categories",), value), F, message)
        for name, value, message in (
            ("empty", [], f"{SPECIALISTS}.finding_categories must be a non-empty array, found an empty array"),
            ("not a list", "Style", _wrong(f"{SPECIALISTS}.finding_categories", "a non-empty array", "Style")),
            ("not text", ["Style", 3], _wrong(f"{SPECIALISTS}.finding_categories[1]", CATEGORY_KIND, 3)),
            ("blank", [" "], _wrong(f"{SPECIALISTS}.finding_categories[0]", CATEGORY_KIND, " ")),
            (
                "padded",
                [" Style"],
                f"{SPECIALISTS}.finding_categories[0] must be {CATEGORY_KIND}, "
                "found a string with whitespace around it",
            ),
            (
                "too long",
                ["x" * 61],
                f"{SPECIALISTS}.finding_categories[0] must be {CATEGORY_KIND}, found a string of 61 characters",
            ),
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
        F,
        _wrong(f"{SPECIALISTS}.resources", "an array", "shared/guide.md"),
    ),
    (
        "an unsafe specialists resource",
        _specialists_manifest,
        _put(("resources", 0), "/shared/guide.md"),
        RuntimeContractError,
        "specialists-manifest.resources[0] is unsafe: '/shared/guide.md'",
    ),
    (
        "conditions that are not an object",
        _specialists_manifest,
        _put(("conditions",), []),
        F,
        _wrong(f"{SPECIALISTS}.conditions", "an object", []),
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
    # A file of a specialists manifest has one role (a top-level resource, a profile, a specialist's guideline, or a
    # condition script), each read from its own commit, and no list names it twice.
    (
        "a profile that is also a resource",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "./shared/guide.md"),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a specialist resource that is also a top-level resource",
        _specialists_manifest,
        _put(("specialists", 0, "resources"), ["shared/guide.md"]),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a specialist resource listed twice",
        _specialists_manifest,
        _put(("specialists", 0, "resources"), ["shared/python.md", "shared/./python.md"]),
        RuntimeContractError,
        TWICE,
    ),
    (
        "one specialist's profile that is another's guideline",
        _specialists_manifest,
        _second_specialist(resources=["agents/python.md"]),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a specialist resource that is a condition script",
        _specialists_manifest,
        _put(("specialists", 0, "resources"), ["conditions/window.py"]),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a profile that is a condition script",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "conditions/window.py"),
        RuntimeContractError,
        TWICE,
    ),
    (
        "a reserved profile",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "materialization.json"),
        RuntimeContractError,
        RESERVED,
    ),
    (
        "a suite profile the suite does not ship",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "suite:security-review"),
        RuntimeContractError,
        "specialists-manifest.specialists[0].profile names no suite profile: 'suite:security-review'; "
        "the suite ships suite:design-review",
    ),
    (
        # Only a catalogued name resolves, so a suite profile cannot reach another file of the suite or anything else.
        "a suite profile that names a path",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "suite:../references/generic-reviewer.md"),
        RuntimeContractError,
        "specialists-manifest.specialists[0].profile names no suite profile: "
        "'suite:../references/generic-reviewer.md'; "
        "the suite ships suite:design-review",
    ),
    (
        "a suite profile in another case, which is a repository path a colon makes unsafe",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), "Suite:design-review"),
        RuntimeContractError,
        "specialists-manifest.specialists[0].profile is unsafe: 'Suite:design-review'",
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
        "condition reads that are not an array",
        _specialists_manifest,
        _put(("conditions", "window", "reads"), "global.json"),
        F,
        _wrong(f"{SPECIALISTS}.conditions.window.reads", "an array of at most 100 items", "global.json"),
    ),
    (
        "condition reads of more than 100 patterns",
        _specialists_manifest,
        _put(("conditions", "window", "reads"), [f"p{index}.json" for index in range(101)]),
        F,
        f"{SPECIALISTS}.conditions.window.reads must be an array of at most 100 items, found an array of 101 items",
    ),
    *[
        (
            f"a condition read {label}",
            _specialists_manifest,
            _put(("conditions", "window", "reads"), ["global.json", pattern]),
            F,
            f"{SPECIALISTS}.conditions.window.reads[1] must be {kind}, found {found}",
        )
        for label, pattern, kind, found in (
            ("that is not a string", 7, READS_KIND, "integer 7"),
            ("that is empty", "", READS_KIND, "an empty string"),
            ("of 201 characters", "a" * 201, READS_KIND, "a string of 201 characters"),
            ("with a backslash", "src\\global.json", GLOB_RULE, 'string "src\\\\global.json"'),
            ("with a control character", "src/\x01.json", GLOB_RULE, 'string "src/\\u0001.json"'),
            ("with a leading slash", "/global.json", GLOB_RULE, 'string "/global.json"'),
            ("with an empty segment", "src//global.json", GLOB_RULE, 'string "src//global.json"'),
            ("with a '.' segment", "./global.json", GLOB_RULE, 'string "./global.json"'),
            ("with a '..' segment", "../global.json", GLOB_RULE, 'string "../global.json"'),
        )
    ],
    (
        "a condition read listed twice in another case",
        _specialists_manifest,
        _put(("conditions", "window", "reads"), ["Global.json", "global.JSON"]),
        F,
        f"{SPECIALISTS}.conditions.window.reads must be distinct glob patterns, ignoring case, "
        'found "global.JSON" twice',
    ),
    (
        "a condition with a field other than script and reads",
        _specialists_manifest,
        _put(("conditions", "window", "writes"), []),
        F,
        _unknown(f"{SPECIALISTS}.conditions.window", "writes"),
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
    ("shape", _put(("extra",), 1), F, _unknown(ENTRYPOINT, "extra")),
    ("protocol", _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    ("id", _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    (
        "supports",
        _put(("supports",), []),
        F,
        f"{ENTRYPOINT}.supports must be a non-empty array, found an empty array",
    ),
    (
        "capabilities",
        _put(("required_capabilities",), [""]),
        F,
        _wrong(f"{ENTRYPOINT}.required_capabilities[0]", "a non-empty string", ""),
    ),
    (
        "entrypoint",
        _put(("entrypoint",), "../SKILL.md"),
        RuntimeContractError,
        "entrypoint-manifest.entrypoint is unsafe: '../SKILL.md'",
    ),
    ("resources array", _put(("resources",), {}), F, _wrong(f"{ENTRYPOINT}.resources", "an array", {})),
    (
        "resources",
        _put(("resources", 0), "../x.md"),
        RuntimeContractError,
        "entrypoint-manifest.resources[0] is unsafe: '../x.md'",
    ),
    ("profiles array", _put(("agent_profiles",), {}), F, _wrong(f"{ENTRYPOINT}.agent_profiles", "an array", {})),
    (
        "profiles",
        _put(("agent_profiles", 0), "../x.md"),
        RuntimeContractError,
        "entrypoint-manifest.agent_profiles[0] is unsafe: '../x.md'",
    ),
    ("duplicates", _put(("agent_profiles", 0), "SKILL.md"), RuntimeContractError, TWICE),
    ("reserved", _put(("resources", 1), "materialization.json"), RuntimeContractError, RESERVED),
]
SPECIALISTS_STAGES: list[ManifestStage] = [
    ("shape", _put(("extra",), 1), F, _unknown(SPECIALISTS, "extra")),
    ("protocol", _put(("protocol_version",), 2), RuntimeContractError, UNSUPPORTED),
    ("id", _put(("id",), "Team"), RuntimeContractError, BAD_ID),
    (
        "supports",
        _put(("supports",), []),
        F,
        f"{SPECIALISTS}.supports must be a non-empty array, found an empty array",
    ),
    (
        "capabilities",
        _put(("required_capabilities",), ["read-diff", "read-diff"]),
        RuntimeContractError,
        BAD_CAPABILITIES,
    ),
    ("kind", _put(("kind",), "entrypoint"), F, _wrong(f"{SPECIALISTS}.kind", 'one of "specialists"', "entrypoint")),
    (
        "uncovered",
        _put(("uncovered",), "skip"),
        F,
        _wrong(f"{SPECIALISTS}.uncovered", 'one of "review", "ignore"', "skip"),
    ),
    ("resources", _put(("resources",), "x"), F, _wrong(f"{SPECIALISTS}.resources", "an array", "x")),
    ("specialists", _put(("conditions",), []), F, _wrong(f"{SPECIALISTS}.conditions", "an object", [])),
    ("duplicates", _put(("resources",), ["a.md", "a.md"]), RuntimeContractError, TWICE),
    ("reserved", _put(("specialists", 0, "profile"), "materialization.json"), RuntimeContractError, RESERVED),
]


MANIFEST_CONTAINERS: list[tuple[str, Callable[[], dict[str, Any]], ManifestMutation, type[Exception], str]] = [
    (
        f"{build.__name__} {field} {value!r}",
        build,
        _put((field,), [value] if field in {"supports", "required_capabilities"} else value),
        F,
        _wrong(path.format(root=root), kind, value),
    )
    for build, root in ((_entrypoint_manifest, ENTRYPOINT), (_specialists_manifest, SPECIALISTS))
    for field, member, path, kind in (
        ("schema_version", 1, "manifest.schema_version", "an integer"),
        ("protocol_version", 1, "{root}.protocol_version", "an integer"),
        ("supports", "initial", "{root}.supports[0]", MODES_KIND),
        ("required_capabilities", "read-diff", "{root}.required_capabilities[0]", "a non-empty string"),
    )
    for value in ([], {}, [member], {"value": member})
] + [
    (
        f"specialist {field} {value!r}",
        _specialists_manifest,
        _put(("specialists", 0, field), value),
        F,
        _wrong(f"{SPECIALIST}.{field}", kind, value),
    )
    for field, member, kind in (
        ("when", "window", "a non-empty string or null"),
        ("model", "opus", MODEL_KIND),
        ("effort", "high", EFFORT_KIND),
    )
    for value in ([], {}, [member], {"value": member})
]

# A wrong-typed value for each kind of field validate_adapter_manifest reads that the cases above leave out, in the
# reader's one shape: strings, arrays, objects, missing and unknown fields, and enumerated values.
MANIFEST_KINDS: list[tuple[str, Callable[[], dict[str, Any]], ManifestMutation, str]] = [
    (
        "schema_version missing",
        _entrypoint_manifest,
        _drop("schema_version"),
        _wrong("manifest.schema_version", "an integer", None),
    ),
    (
        "entrypoint id",
        _entrypoint_manifest,
        _put(("id",), False),
        _wrong(f"{ENTRYPOINT}.id", "a non-empty string", False),
    ),
    (
        "entrypoint",
        _entrypoint_manifest,
        _put(("entrypoint",), 5),
        _wrong(f"{ENTRYPOINT}.entrypoint", "a non-empty string", 5),
    ),
    (
        "a resource",
        _entrypoint_manifest,
        _put(("resources", 0), None),
        _wrong(f"{ENTRYPOINT}.resources[0]", "a non-empty string", None),
    ),
    (
        "an agent profile",
        _entrypoint_manifest,
        _put(("agent_profiles", 0), " "),
        _wrong(f"{ENTRYPOINT}.agent_profiles[0]", "a non-empty string", " "),
    ),
    ("specialists id", _specialists_manifest, _put(("id",), []), _wrong(f"{SPECIALISTS}.id", "a non-empty string", [])),
    ("kind", _specialists_manifest, _put(("kind",), 2), _wrong(f"{SPECIALISTS}.kind", 'one of "specialists"', 2)),
    (
        "uncovered",
        _specialists_manifest,
        _put(("uncovered",), True),
        _wrong(f"{SPECIALISTS}.uncovered", 'one of "review", "ignore"', True),
    ),
    (
        "fallback_finding_category",
        _specialists_manifest,
        _put(("fallback_finding_category",), 5),
        _wrong(f"{SPECIALISTS}.fallback_finding_category", "a non-empty string", 5),
    ),
    (
        "a top-level resource",
        _specialists_manifest,
        _put(("resources", 0), {}),
        _wrong(f"{SPECIALISTS}.resources[0]", "a non-empty string", {}),
    ),
    (
        "specialists",
        _specialists_manifest,
        _put(("specialists",), {}),
        _wrong(f"{SPECIALISTS}.specialists", "a non-empty array", {}),
    ),
    (
        "no specialists",
        _specialists_manifest,
        _put(("specialists",), []),
        f"{SPECIALISTS}.specialists must be a non-empty array, found an empty array",
    ),
    ("a specialist", _specialists_manifest, _put(("specialists", 0), "x"), _wrong(SPECIALIST, "an object", "x")),
    (
        "a specialist without its fields",
        _specialists_manifest,
        _put(("specialists", 0), {"id": "x"}),
        _missing(SPECIALIST, "category", "exclude", "include", "profile", "resources", "when"),
    ),
    (
        "a specialist's unknown field",
        _specialists_manifest,
        _put(("specialists", 0, "extra"), 1),
        _unknown(SPECIALIST, "extra"),
    ),
    (
        "a specialist id",
        _specialists_manifest,
        _put(("specialists", 0, "id"), 5),
        _wrong(f"{SPECIALIST}.id", "a non-empty string", 5),
    ),
    (
        "a specialist category",
        _specialists_manifest,
        _put(("specialists", 0, "category"), " "),
        _wrong(f"{SPECIALIST}.category", "a non-empty string", " "),
    ),
    (
        "a profile",
        _specialists_manifest,
        _put(("specialists", 0, "profile"), []),
        _wrong(f"{SPECIALIST}.profile", "a non-empty string", []),
    ),
    (
        "include",
        _specialists_manifest,
        _put(("specialists", 0, "include"), "x"),
        _wrong(f"{SPECIALIST}.include", "a non-empty array", "x"),
    ),
    (
        "an empty include",
        _specialists_manifest,
        _put(("specialists", 0, "include"), []),
        f"{SPECIALIST}.include must be a non-empty array, found an empty array",
    ),
    (
        "an include pattern",
        _specialists_manifest,
        _put(("specialists", 0, "include", 0), 5),
        _wrong(f"{SPECIALIST}.include[0]", "a non-empty string", 5),
    ),
    (
        "exclude",
        _specialists_manifest,
        _put(("specialists", 0, "exclude"), {}),
        _wrong(f"{SPECIALIST}.exclude", "an array", {}),
    ),
    (
        "a specialist resource",
        _specialists_manifest,
        _put(("specialists", 0, "resources"), [None]),
        _wrong(f"{SPECIALIST}.resources[0]", "a non-empty string", None),
    ),
    (
        "when",
        _specialists_manifest,
        _put(("specialists", 0, "when"), 5),
        _wrong(f"{SPECIALIST}.when", "a non-empty string or null", 5),
    ),
    (
        "model",
        _specialists_manifest,
        _put(("specialists", 0, "model"), "gpt"),
        _wrong(f"{SPECIALIST}.model", MODEL_KIND, "gpt"),
    ),
    (
        "effort",
        _specialists_manifest,
        _put(("specialists", 0, "effort"), True),
        _wrong(f"{SPECIALIST}.effort", EFFORT_KIND, True),
    ),
    (
        "a condition",
        _specialists_manifest,
        _put(("conditions", "window"), "x"),
        _wrong(f"{SPECIALISTS}.conditions.window", "an object", "x"),
    ),
    (
        "a condition without a script",
        _specialists_manifest,
        _put(("conditions", "window"), {}),
        _missing(f"{SPECIALISTS}.conditions.window", "script"),
    ),
    (
        "a condition script",
        _specialists_manifest,
        _put(("conditions", "window", "script"), 5),
        _wrong(f"{SPECIALISTS}.conditions.window.script", "a non-empty string", 5),
    ),
]


class AdapterManifestValidationTests(GitTestCase):
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
        for name, build, mutation, error, message in MANIFEST_REJECTED + MANIFEST_CONTAINERS:
            with self.subTest(name):
                self.assert_refused(mutation(build()), error, message)

    def test_each_kind_of_field_refuses_a_wrong_type(self) -> None:
        for name, build, mutation, message in MANIFEST_KINDS:
            with self.subTest(name):
                self.assert_refused(mutation(build()), F, message)

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

    def test_manifest_loaders_refuse_container_fields_with_the_same_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "manifest.json"
            for name, build, mutation, error, message in MANIFEST_CONTAINERS:
                content = json.dumps(mutation(build()))
                path.write_text(content, encoding="utf-8")
                for source in ("file", "commit"):
                    with self.subTest(name=name, source=source):
                        with (
                            mock.patch.object(review_runtime, "_read_git_file", return_value=content.encode("utf-8")),
                            self.assertRaises(error) as caught,
                        ):
                            if source == "file":
                                review_runtime.load_manifest_from_file(path)
                            else:
                                review_runtime.load_manifest_from_commit(root, HEAD, "manifest.json")
                        self.assertIs(error, type(caught.exception))
                        self.assertEqual(message, str(caught.exception))


class ReservedPathMaterializationTests(GitTestCase):
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


# build_adapter_request's arguments of the wrong kind: (name, the argument replaced, the message).
REQUEST_KINDS: list[tuple[str, dict[str, Any], str]] = [
    ("mode", {"mode": "full"}, _wrong("request.mode", MODES_KIND, "full")),
    ("mode null", {"mode": None}, _wrong("request.mode", MODES_KIND, None)),
    ("pull number", {"pull_number": True}, _wrong("request.pull_number", "an integer of at least 1", True)),
    ("pull number float", {"pull_number": 1.0}, _wrong("request.pull_number", "an integer of at least 1", 1.0)),
    ("pull number zero", {"pull_number": 0}, _wrong("request.pull_number", "an integer of at least 1", 0)),
    ("base sha", {"base_sha": 5}, _wrong("request.pull_request.base_sha", COMMIT_KIND, 5)),
    (
        "head sha",
        {"head_sha": "A" * 40},
        f"request.pull_request.head_sha must be {COMMIT_KIND}, found {NOT_HEX}",
    ),
    (
        "body characters",
        {"body_characters": "10"},
        _wrong("request.pull_request.body_characters", "an integer of at least 0 or null", "10"),
    ),
    (
        "negative body characters",
        {"body_characters": -1},
        "request.pull_request.body_characters must be an integer of at least 0 or null, found integer -1",
    ),
]


class AdapterRequestArgumentTests(GitTestCase):
    def test_each_argument_of_the_wrong_kind_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            diff, body = root / "pull.diff", root / "body.md"
            diff.write_text("", encoding="utf-8")
            body.write_text("Body", encoding="utf-8")
            arguments: dict[str, Any] = {
                "mode": "initial",
                "repository": "owner/repo",
                "pull_number": 7,
                "base_ref": "main",
                "base_sha": "b" * 40,
                "head_sha": HEAD,
                "title": "Title",
                "url": "https://github.com/owner/repo/pull/7",
                "diff_path": diff,
                "source_snapshot_root": root / "source",
                "snapshot": _snapshot().metadata,
                "body_file": body,
                "body_characters": 4,
            }
            self.assertEqual(7, review_runtime.build_adapter_request(**arguments)["pull_number"])
            for name, changes, message in REQUEST_KINDS:
                with self.subTest(name), self.assertRaises(Exception) as caught:
                    review_runtime.build_adapter_request(**{**arguments, **changes})
                self.assertIs(F, type(caught.exception))
                self.assertEqual(message, str(caught.exception))


def _listing(**entry: Any) -> dict[str, Any]:
    """A GitHub tree listing of one blob, with the given fields of the entry replaced."""
    return {"truncated": False, "tree": [{"type": "blob", "path": "a.txt", "mode": "100644", "sha": HEAD, **entry}]}


ENTRY = "tree listing.tree[0]"
# A tree listing with a field of the wrong kind, as GitHub's trees API might answer: (name, listing, message).
LISTING_KINDS: list[tuple[str, Any, str]] = [
    ("listing", [], _wrong("tree listing", "an object", [])),
    ("tree missing", {"truncated": False}, _wrong("tree listing.tree", "an array", None)),
    ("tree", {"truncated": False, "tree": {}}, _wrong("tree listing.tree", "an array", {})),
    ("entry", {"truncated": False, "tree": ["x"]}, _wrong(ENTRY, "an object", "x")),
    ("type", _listing(type="file"), _wrong(f"{ENTRY}.type", 'one of "blob", "tree", "commit"', "file")),
    ("path", _listing(path=5), _wrong(f"{ENTRY}.path", "a non-empty string", 5)),
    ("empty path", _listing(path=""), _wrong(f"{ENTRY}.path", "a non-empty string", "")),
    ("mode", _listing(mode=None), _wrong(f"{ENTRY}.mode", "a string", None)),
    ("sha", _listing(sha=[]), _wrong(f"{ENTRY}.sha", COMMIT_KIND, [])),
    ("uppercase sha", _listing(sha="A" * 40), f"{ENTRY}.sha must be {COMMIT_KIND}, found {NOT_HEX}"),
]


class TreeListingTests(GitTestCase):
    def test_a_listing_reads_each_blob(self) -> None:
        listing = _listing()
        listing["tree"].append({"type": "tree", "path": "src", "mode": "040000", "sha": "c" * 40})
        listing["tree"].append({"type": "blob", "path": "link", "mode": "120000", "sha": "d" * 40})
        blobs = review_runtime._listed_blobs(listing)
        self.assertEqual({("a.txt", "file", HEAD): 1, ("link", "symbolic-link", "d" * 40): 1}, dict(blobs))

    def test_each_field_of_the_wrong_kind_is_refused(self) -> None:
        for name, listing, message in LISTING_KINDS:
            with self.subTest(name), self.assertRaises(Exception) as caught:
                review_runtime._listed_blobs(listing)
            self.assertIs(F, type(caught.exception))
            self.assertEqual(message, str(caught.exception))

    def test_a_truncated_listing_is_refused_as_a_value(self) -> None:
        for truncated in (True, None, 0):
            with self.subTest(truncated=truncated), self.assertRaises(Exception) as caught:
                review_runtime._listed_blobs({**_listing(), "truncated": truncated})
            self.assertIs(RuntimeContractError, type(caught.exception))
            self.assertEqual("GitHub truncated the tree listing", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

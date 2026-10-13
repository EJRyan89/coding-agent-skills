"""What this skill's conditions share: the variables a review sets for a condition, the reader of the diff it names,
the scripts they read, and this repository's rule for a suite.

doc_passages.py and untested_functions.py import it, and the specialists manifest declares it as a resource, so it
sits beside them in the reviewer folder a review materializes.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

from diff_paths import header_path, snapshot_path, unquote

# The pull request's unified diff, and the file a condition writes for its route's reviewer.
DIFF_VARIABLE = "CODE_REVIEW_DIFF_FILE"
OUTPUT_VARIABLE = "CODE_REVIEW_CONDITION_OUTPUT"
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh")
# Validation's rule for a suite (TEST_NAME_PATTERNS in tests/validation/validation_support.py), matched ignoring case
# against a file's name or its stem.
SUITE_NAMES = ("test_*", "test-*", "*_test", "*-test", "*.test.*")


def is_suite(relative: str) -> bool:
    """Whether a repository path names a suite by validation's rule: its name or stem, ignoring case."""
    name = relative.rsplit("/", 1)[-1].casefold()
    stem, dot, suffix = name.rpartition(".")
    if not dot or f".{suffix}" not in SCRIPT_SUFFIXES:
        return False
    return any(fnmatch.fnmatchcase(text, pattern) for pattern in SUITE_NAMES for text in (stem, name))


@dataclass(frozen=True)
class FileDiff:
    """One file's part of a unified diff: the paths its headers name, where the head's lands in the snapshot, and
    its hunks."""

    old: str | None  # the path before the change: None for an added file
    new: str | None  # the path at the head: None for a deleted file
    head: Path | None  # where `new` lands in the snapshot, the one place a condition reads the file from
    lines: tuple[str, ...]  # its lines from its first hunk header on

    @property
    def path(self) -> str:
        """The path a reviewer knows the file by: the head's, or a deleted file's old one."""
        return self.new or self.old or ""


def read_files(text: str, root: Path) -> tuple[list[FileDiff], list[str]]:
    """Each file a unified diff changes, in diff order, and each path refused, once.

    A file's paths come from its `---`, `+++`, `rename from`, and `rename to` lines before its first hunk, decoded
    through diff_paths; inside a hunk, an added line that begins `++ ` or a removed one that begins `-- ` is content.
    A file any of whose paths `snapshot_path` refuses, as one that could land outside the snapshot at `root`, is left
    out whole and its path refused, so no condition reads it or names it."""
    files: list[FileDiff] = []
    refused: list[str] = []
    for section in re.split(r"^diff --git .*$", text.replace("\r\n", "\n"), flags=re.MULTILINE):
        lines = section.split("\n")
        first = next((index for index, line in enumerate(lines) if line.startswith("@@")), len(lines))
        old = new = None
        for line in lines[:first]:
            if line.startswith("--- "):
                old = header_path(line[4:])
            elif line.startswith("+++ "):
                new = header_path(line[4:])
            elif line.startswith("rename from "):
                old = unquote(line[len("rename from ") :])
            elif line.startswith("rename to "):
                new = unquote(line[len("rename to ") :])
        if old is None and new is None:
            continue
        landed = {path: snapshot_path(root, path) for path in (new, old) if path is not None}
        unsafe = next((path for path, place in landed.items() if place is None), None)
        if unsafe is not None:
            if unsafe not in refused:
                refused.append(unsafe)
            continue
        files.append(FileDiff(old, new, None if new is None else landed[new], tuple(lines[first:])))
    return files, refused


def refusal_line(path: str) -> str:
    """The line a condition prints for a path `read_files` refused, the path quoted as JSON so it stays one line."""
    return f"REFUSED {json.dumps(path)}: a path that could name a file outside the snapshot"

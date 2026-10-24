"""What this skill's conditions share: the variables a review sets for a condition, the reader of the diff it names,
the scripts they read, this repository's rule for a suite, and how a condition fails.

doc_passages.py and untested_functions.py import it, and the specialists manifest declares it as a resource, so it
sits beside them in the reviewer folder a review materializes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from console import failed_line
from diff_paths import header_path, snapshot_path, unquote
from suite_names import is_suite_name

# The variables are ejryan89-code-review-core's side of the condition protocol as much as this one's.
DUPLICATION_ALLOWED = {
    "DIFF_VARIABLE": (
        "skills/ejryan89-code-review-core/scripts/review_specialists.py sets it for a condition; a condition runs from "
        "a "
        "reviewer folder holding only the manifest's declared resources, and that module's imports reach most of "
        "ejryan89-code-review-core, so each side of the protocol names it"
    ),
    "OUTPUT_VARIABLE": (
        "skills/ejryan89-code-review-core/scripts/review_specialists.py sets it for a condition; a condition runs from "
        "a "
        "reviewer folder holding only the manifest's declared resources, and that module's imports reach most of "
        "ejryan89-code-review-core, so each side of the protocol names it"
    ),
}

# The pull request's unified diff, and the file a condition writes for its route's reviewer.
DIFF_VARIABLE = "CODE_REVIEW_DIFF_FILE"
OUTPUT_VARIABLE = "CODE_REVIEW_CONDITION_OUTPUT"
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh")
# A condition's exit status 1 closes its route, so a failure exits with this instead and fails the review.
FAILURE_STATUS = 2


def is_suite(relative: str) -> bool:
    """Whether a repository path names a suite by validation's rule, ejryan89-skill-core's suite_names."""
    return is_suite_name(relative.rsplit("/", 1)[-1])


def unset_variables_line() -> str | None:
    """The FAILED line for the variables a review sets for a condition that are not set, or None when both are."""
    missing = [variable for variable in (DIFF_VARIABLE, OUTPUT_VARIABLE) if not os.environ.get(variable)]
    return f"FAILED {' and '.join(missing)} not set; a review sets them for a condition" if missing else None


class MissingEvidence(Exception):
    """Evidence a condition reads that the snapshot does not hold. It fails the review, as run_condition reports any
    failure, rather than closing the route, where "nothing found" would mean "nothing looked at"."""


def require_snapshot(root: Path) -> None:
    """Raise MissingEvidence unless the snapshot at `root` is a directory."""
    if not root.is_dir():
        raise MissingEvidence(f"the snapshot {json.dumps(root.as_posix())} is not a directory")


def run_condition(run: Callable[[], int]) -> int:
    """A condition's exit status from its work, a failure printed as a FAILED line and ending with FAILURE_STATUS,
    since the exit status 1 run_main would give it closes the route rather than failing the review."""
    try:
        return run()
    except Exception as error:  # noqa: BLE001 - a condition reports any failure as its FAILED line and FAILURE_STATUS
        print(failed_line(error))
        return FAILURE_STATUS


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

"""What this skill's conditions share: the variables a review sets for a condition, the scripts they read, and this
repository's rule for a suite.

doc_passages.py and untested_functions.py import it, and the specialists manifest declares it as a resource, so it
sits beside them in the reviewer folder a review materializes.
"""

from __future__ import annotations

import fnmatch

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

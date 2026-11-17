"""This repository's rule for a regression suite: a file named `test_*`, `test-*`, `*_test`, `*-test`, or `*.test.*`,
matched against its stem or its whole name ignoring case, with a `.py`, `.sh`, or `.ps1` extension.

Validation finds the suites it runs by it and the deployer leaves a skill's suites out of what it deploys by it, and
every other reader that tells a suite from other code, such as the review-repository conditions, audit-skill's
inventory, and tools/skill_evals_scenarios.py's copy of a reviewer's files, imports it, so they all read one rule.
"""

from __future__ import annotations

import fnmatch

SUITE_NAME_PATTERNS = ("test_*", "test-*", "*_test", "*-test", "*.test.*")
SUITE_EXTENSIONS = frozenset({".py", ".sh", ".ps1"})


def is_suite_name(name: str) -> bool:
    """Whether a file's name, without its folder, names a suite by the rule."""
    folded = name.casefold()
    stem, dot, suffix = folded.rpartition(".")
    if not dot or not stem or f".{suffix}" not in SUITE_EXTENSIONS:
        return False
    return any(fnmatch.fnmatchcase(text, pattern) for pattern in SUITE_NAME_PATTERNS for text in (stem, folded))

"""Ask GitHub whether each issue the malformed-input known failures or PENDING name is an open issue.

Usage: python -B tests/known_failure_issues.py

This is the one part of validation that reads GitHub, so it is a program of its own: it asks, through gh, whether each
issue tests/malformed_input/known_failures.json or PENDING in tests/malformed_input/readers.py names is an open issue,
prints a FAILED line for each that is not, is a pull request, or gh cannot answer about, and exits 1 when there is
one; it asks nothing while both are empty. It imports tests/validation/known_failures.py and what that imports, and
none of the policy checks or suites tests/run_validation.py runs, so the token validate.yml gives the one step that
runs it reaches that query's own modules alone; tests/test_known_failure_issues.py pins the set it loads.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ejryan89-skill-core" / "scripts"))

import known_failures
from console import run_main
from validation_support import REPOSITORY_ROOT


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    """Print each issue a known failure or PENDING names that GitHub does not say is open, and return 1 when there is
    one; any argument is a usage error, exit 2, since it takes none."""
    argparse.ArgumentParser(description="Ask GitHub whether each known failure's issue is open.").parse_args(argv)
    out = sys.stdout if out is None else out
    problems = known_failures.open_issue_problems(REPOSITORY_ROOT)
    for problem in problems:
        print(f"FAILED {problem}", file=out)
    print(f"Known-failure issues: {len(problems)} not shown open.", file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    run_main(main)

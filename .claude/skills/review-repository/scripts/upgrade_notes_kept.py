"""The condition in front of this repository's upgrade-notes reviewer: whether the reviewed head keeps upgrade notes.

Usage:
  python upgrade_notes_kept.py --source-root <snapshot>

review-prs runs it, as the specialists manifest beside it declares, on the source snapshot of the pull request's
head, when a changed file matches the upgrade-notes reviewer's routes. The manifest declares the one file it reads,
docs/upgrade-notes.md, so the snapshot stays lazy: the script sees only the changed files, the analyzer settings, and
that file.

The reviewer judges whether the head's `## Unreleased` section names each contract item the change alters, at the
right level. When the head keeps no such section there is nothing to judge an entry against, so the reviewer is
skipped and the documentation reviewer, which every change to docs/upgrade-notes.md reaches, judges the notes
themselves. It prints one line:

  OPEN <reason>       exit 0: the reviewer runs
  CLOSED <reason>     exit 1: the reviewer is skipped

Any other exit, such as 2 for a usage error, fails the review.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

from console import use_utf8_output

# The notes file, relative to the snapshot's root; the manifest's `reads` for this condition names the same path.
NOTES = "docs/upgrade-notes.md"
UNRELEASED = re.compile(r"^## Unreleased[ \t]*$", re.MULTILINE)


def keeps_notes(source_root: Path) -> tuple[bool, str]:
    """Whether the snapshot's upgrade notes have an `## Unreleased` section, and why."""
    path = source_root.joinpath(*NOTES.split("/"))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, f"{NOTES} is not in the head"
    except (OSError, UnicodeDecodeError) as exc:
        # An unreadable file is the reviewer's to judge, not a reason to skip it.
        return True, f"{NOTES} cannot be read: {exc}"
    if UNRELEASED.search(text.replace("\r\n", "\n")):
        return True, f"{NOTES} keeps an Unreleased section"
    return False, f"{NOTES} has no Unreleased section"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Whether the reviewed head keeps upgrade notes.")
    parser.add_argument("--source-root", required=True, type=Path)
    options = parser.parse_args(argv)
    if not options.source_root.is_dir():
        parser.error(f"--source-root is not a directory: {options.source_root}")
    kept, reason = keeps_notes(options.source_root)
    print(f"{'OPEN' if kept else 'CLOSED'} {reason}")
    return 0 if kept else 1


if __name__ == "__main__":
    use_utf8_output(errors="replace", newline="\n")
    raise SystemExit(main())

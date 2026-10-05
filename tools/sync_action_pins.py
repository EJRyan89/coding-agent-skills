"""Copy the reviewed GitHub Actions pins from validate.yml into the init-ai-config generator, its test, and references.

Usage:
  python tools/sync_action_pins.py           rewrite every stale pin; exit 1 if a pin cannot be synced
  python tools/sync_action_pins.py --check   report every stale file and change nothing; exit 1 if there is one

Dependabot updates only .github/workflows/validate.yml, whose pins are the reviewed ones. init-ai-config generates
workflows that pin the same actions, and tests/ai-config/test_cross_skill_contracts.py fails until they match, so
run this after merging or on the branch of a Dependabot pull request. Each pin's SHA and its `# vX.Y.Z` comment are
replaced; the spacing around the comment is kept.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = Path(".github/workflows/validate.yml")
SKILL = Path("skills/init-ai-config")
TARGETS = (SKILL / "scripts/ai_config_template.py", SKILL / "scripts/test_ai_config_template.py")
REFERENCES = SKILL / "references"
COMMAND = "python tools/sync_action_pins.py"
VERSION = r"v\d+(?:\.\d+)*"
REVIEWED_PIN = re.compile(r"uses:\s*(actions/[A-Za-z0-9_.-]+)@([0-9a-f]{40})(?:[ \t]*#[ \t]*(" + VERSION + r"))?")
# The comment pattern never runs past the version: in the generator the pin sits inside a string ending in `\n"`.
PIN = re.compile(r"(actions/[A-Za-z0-9_.-]+)@([0-9a-f]{40})(?:([ \t]*#[ \t]*)(" + VERSION + r"))?")


class SyncError(Exception):
    pass


def reviewed_pins(root: Path) -> dict[str, tuple[str, str]]:
    """Each action validate.yml pins, with its SHA and version comment."""
    pins: dict[str, tuple[str, str]] = {}
    for action, sha, version in REVIEWED_PIN.findall((root / WORKFLOW).read_text(encoding="utf-8")):
        if not version:
            raise SyncError(f"{WORKFLOW.as_posix()}: {action} has no version comment beside its SHA")
        pins[action] = (sha, version)
    if not pins:
        raise SyncError(f"{WORKFLOW.as_posix()} pins no actions/* action to a commit SHA")
    return pins


def targets(root: Path) -> list[Path]:
    return [*TARGETS, *sorted(path.relative_to(root) for path in (root / REFERENCES).glob("*.yml"))]


def synced(text: str, pins: dict[str, tuple[str, str]], relative: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        action, _, spacing, _ = match.groups()
        if action not in pins:
            raise SyncError(f"{relative.as_posix()} pins {action}, which {WORKFLOW.as_posix()} does not pin")
        sha, version = pins[action]
        return f"{action}@{sha}" + (f"{spacing}{version}" if spacing is not None else "")

    return PIN.sub(replace, text)


def stale_files(root: Path) -> dict[Path, str]:
    """Every target whose pins differ from validate.yml, with its synced text."""
    pins = reviewed_pins(root)
    stale: dict[Path, str] = {}
    for relative in targets(root):
        path = root / relative
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        updated = synced(text, pins, relative)
        if updated != text:
            stale[relative] = updated
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="report stale files without changing them")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        stale = stale_files(arguments.root)
    except (OSError, SyncError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 1
    if not stale:
        print("action pins are in sync with validate.yml")
        return 0
    if arguments.check:
        for relative in stale:
            print(f"stale {relative.as_posix()}")
        print(f"run {COMMAND} to copy the pins from {WORKFLOW.as_posix()}")
        return 1
    for relative, text in stale.items():
        with (arguments.root / relative).open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print(f"updated {relative.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

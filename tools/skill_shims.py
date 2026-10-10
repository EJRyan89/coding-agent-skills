"""Keep each repository skill's .agents/skills shim in step with the skill under .claude/skills.

Usage:
  python tools/skill_shims.py           report every missing, stale, or stray shim; exit 1 if there is one
  python tools/skill_shims.py --write   write each missing or stale shim from its skill's frontmatter

Codex and Copilot CLI find this repository's own skills through .agents/skills/<name>/SKILL.md, which carries the
skill's frontmatter unchanged, since that is all a runtime reads before choosing the skill, and two lines pointing at
the skill as the authoritative workflow. This is the one writer of those shims: validation compares each shim with
what it would write. --write never removes a stray shim, whose skill was renamed or removed; it reports it, and a
person deletes it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

import frontmatter
from console import use_utf8_output

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILLS = Path(".claude") / "skills"
SHIMS = Path(".agents") / "skills"
WRITE_COMMAND = "python tools/skill_shims.py --write"


def shim_path(name: str) -> Path:
    return SHIMS / name / "SKILL.md"


def shim_text(name: str, skill_text: str) -> str | None:
    """The shim for the repository skill whose SKILL.md is skill_text, or None when it has no closed frontmatter."""
    try:
        found = frontmatter.split(skill_text.splitlines())
    except frontmatter.FrontmatterError:
        return None
    if not found or not found[0]:
        return None
    skill = f"../../../{SKILLS.as_posix()}/{name}"
    return "\n".join(
        [
            frontmatter.DELIMITER,
            *found[0],
            frontmatter.DELIMITER,
            "",
            f"Read and follow `{skill}/SKILL.md` as the authoritative workflow.",
            f"Resolve all relative paths and supporting resources from `{skill}/`.",
            "",
        ]
    )


def _skills(root: Path) -> dict[str, Path]:
    return {path.parent.name: path for path in (root / SKILLS).glob("*/SKILL.md")}


def _expected(root: Path) -> dict[str, str | None]:
    return {name: shim_text(name, path.read_text(encoding="utf-8-sig")) for name, path in _skills(root).items()}


def problems(root: Path) -> list[str]:
    """Each repository skill without frontmatter, without a shim, or with a shim that differs from what --write
    writes, and each shim without a skill."""
    expected = _expected(root)
    shims = {path.parent.name for path in (root / SHIMS).glob("*/SKILL.md")}
    found = [
        f"{shim_path(name).as_posix()} has no {(SKILLS / name / 'SKILL.md').as_posix()}; delete it"
        for name in sorted(shims - set(expected))
    ]
    for name, text in sorted(expected.items()):
        skill = (SKILLS / name / "SKILL.md").as_posix()
        if text is None:
            found.append(f"{skill} has no closed frontmatter for its shim to carry")
        elif name not in shims:
            found.append(f"{skill} has no {shim_path(name).as_posix()} shim; run {WRITE_COMMAND}")
        elif (root / shim_path(name)).read_text(encoding="utf-8-sig").replace("\r\n", "\n") != text:
            found.append(f"{shim_path(name).as_posix()} differs from what {WRITE_COMMAND} writes; run it")
    return found


def write(root: Path, names: list[str] | None = None) -> list[Path]:
    """Write each missing or stale shim, of the named skills or of every skill, and return the paths written."""
    written = []
    for name, text in sorted(_expected(root).items()):
        if text is None or (names is not None and name not in names):
            continue
        path = root / shim_path(name)
        if path.is_file() and path.read_text(encoding="utf-8-sig").replace("\r\n", "\n") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(shim_path(name))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="write each missing or stale shim")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        written = write(arguments.root) if arguments.write else []
        found = problems(arguments.root)
    except OSError as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(f"WROTE {path.as_posix()}")
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    use_utf8_output(errors="backslashreplace")
    sys.exit(main())

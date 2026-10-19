"""Keep each repository skill's .agents/skills shim in step with the skill under .claude/skills.

Usage:
  python tools/skill_shims.py           report every missing, stale, or stray shim file; exit 1 if there is one
  python tools/skill_shims.py --write   write each missing or stale shim file from its skill's frontmatter

Codex and Copilot CLI find this repository's own skills through .agents/skills/<name>/. A shim folder holds SKILL.md,
which carries the skill's frontmatter unchanged, the part of SKILL.md a runtime reads before choosing the skill, a
line pointing at the skill as the authoritative workflow, a line saying its commands and paths resolve from the
repository root, and a line saying a write under .agents needs an approval under Codex's sandbox. Codex reads a
skill's invocation policy from agents/openai.yaml beside SKILL.md rather than from its frontmatter, so the shim of a
skill only the user starts (disable-model-invocation: true) also holds that file with the policy the deployer writes
into a user-only skill's runtime adapter, and Codex never starts the skill implicitly. This is the one writer of
those shims: validation compares every file in each shim folder with what it would write, and reports any other file
there, a folder with no SKILL.md, and anything else under .agents/skills. --write never removes a stray shim, whose
skill was renamed or removed, or a stray file; it reports them, and a person deletes them.

--root DIR works on another checkout of this repository, as the regression suite does. It refuses, as a usage
error, a directory that does not hold source.json and .claude/skills, or that lies in a skills directory, such as
~/.agents/skills, whose adapters the deployer owns. A file it cannot read or write ends the run with a FAILED line
and exit 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
from console import run_main

from deployer.render import CODEX_METADATA, CODEX_USER_ONLY
from tools.checkout import ROOT_HELP, checkout_root

# change-skill names this command among the regenerations a change owes, from the checkout without the deployer.
DUPLICATION_ALLOWED = {
    "WRITE_COMMAND": (
        ".claude/skills/change-skill/scripts/change_skill.py names it among what a change owes, and runs without "
        "the deployer, which this module imports"
    ),
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILLS = Path(".claude") / "skills"
SHIMS = Path(".agents") / "skills"
SKILL_FILE = "SKILL.md"
WRITE_COMMAND = "python tools/skill_shims.py --write"
# A repository skill writes its commands and paths from the repository root, never from its own folder.
ROOT_RELATIVE = (
    "Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every "
    "relative path it names from there."
)
# Codex keeps .agents read-only inside a workspace-write sandbox, and the shim writers write there.
PROTECTED_WRITES = (
    "A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps "
    "`.agents` read-only."
)


def shim_path(name: str) -> Path:
    return SHIMS / name / SKILL_FILE


def shim_text(name: str, skill_text: str) -> str | None:
    """The shim's SKILL.md for the repository skill whose SKILL.md is skill_text, or None when it has no closed
    frontmatter."""
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
            f"Read and follow `{skill}/SKILL.md`, a path relative to this file, as the authoritative workflow.",
            ROOT_RELATIVE,
            PROTECTED_WRITES,
            "",
        ]
    )


def user_only(skill_text: str) -> bool:
    """Whether only the user may start the skill, as the deployer reads disable-model-invocation for an adapter."""
    return (frontmatter.parse(skill_text).string("disable-model-invocation") or "").casefold() == "true"


def shim_files(name: str, skill_text: str) -> dict[str, str] | None:
    """Every file of the skill's shim folder, by its path inside the folder, or None when the skill has no closed
    frontmatter. Raises FrontmatterError when the frontmatter cannot be read."""
    text = shim_text(name, skill_text)
    if text is None:
        return None
    files = {SKILL_FILE: text}
    if user_only(skill_text):
        files[CODEX_METADATA] = CODEX_USER_ONLY.decode("utf-8")
    return files


def _skills(root: Path) -> dict[str, Path]:
    return {path.parent.name: path for path in (root / SKILLS).glob(f"*/{SKILL_FILE}")}


def _expected(root: Path) -> dict[str, dict[str, str] | str | None]:
    """Each skill's shim files; None for a skill with no closed frontmatter, and the reason for one whose frontmatter
    cannot be read."""
    expected: dict[str, dict[str, str] | str | None] = {}
    for name, path in _skills(root).items():
        try:
            expected[name] = shim_files(name, path.read_text(encoding="utf-8-sig"))
        except frontmatter.FrontmatterError as exc:
            expected[name] = str(exc)
    return expected


def _current(path: Path, text: str) -> bool:
    """Whether the file holds text, whatever its line endings; a file that is not UTF-8 does not."""
    try:
        return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n") == text
    except UnicodeError:
        return False


def _folder_files(folder: Path) -> list[str]:
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if not path.is_dir())


def _stray_problems(root: Path, expected: dict[str, dict[str, str] | str | None]) -> list[str]:
    """Each entry under .agents/skills that is not a shim folder, and each shim folder whose skill is gone."""
    found = []
    shims = root / SHIMS
    for entry in sorted(shims.iterdir()) if shims.is_dir() else []:
        relative = (SHIMS / entry.name).as_posix()
        if not entry.is_dir():
            found.append(f"{relative} is not a shim folder; delete it")
        elif entry.name not in expected:
            shown = shim_path(entry.name) if (entry / SKILL_FILE).is_file() else SHIMS / entry.name
            found.append(f"{shown.as_posix()} has no {(SKILLS / entry.name / SKILL_FILE).as_posix()}; delete it")
    return found


def problems(root: Path) -> list[str]:
    """Each repository skill without frontmatter, without a shim, or with a shim file that differs from what --write
    writes, each file in a shim folder that --write does not write, and each shim folder or other entry under
    .agents/skills without a skill."""
    expected = _expected(root)
    found = _stray_problems(root, expected)
    for name, files in sorted(expected.items()):
        skill = (SKILLS / name / SKILL_FILE).as_posix()
        folder = root / SHIMS / name
        if files is None:
            found.append(f"{skill} has no closed frontmatter for its shim to carry")
            continue
        if isinstance(files, str):
            found.append(f"{skill} has frontmatter its shim cannot carry: {files}")
            continue
        if not (folder / SKILL_FILE).is_file():
            found.append(f"{skill} has no {shim_path(name).as_posix()} shim; run {WRITE_COMMAND}")
            continue
        for relative, text in sorted(files.items()):
            path = (SHIMS / name / relative).as_posix()
            if not (folder / relative).is_file():
                found.append(f"{path} is missing from the shim; run {WRITE_COMMAND}")
            elif not _current(folder / relative, text):
                found.append(f"{path} differs from what {WRITE_COMMAND} writes; run it")
        for relative in _folder_files(folder):
            if relative not in files:
                found.append(f"{(SHIMS / name / relative).as_posix()} is no file {WRITE_COMMAND} writes; delete it")
    return found


def write(root: Path, names: list[str] | None = None) -> list[Path]:
    """Write each missing or stale shim file, of the named skills or of every skill, and return the paths written."""
    written = []
    for name, files in sorted(_expected(root).items()):
        if not isinstance(files, dict) or (names is not None and name not in names):
            continue
        for relative, text in sorted(files.items()):
            path = root / SHIMS / name / relative
            if path.is_file() and _current(path, text):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(SHIMS / name / relative)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="write each missing or stale shim file")
    parser.add_argument("--root", type=checkout_root, default=REPOSITORY_ROOT, metavar="DIR", help=ROOT_HELP)
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
    run_main(main, errors="backslashreplace")

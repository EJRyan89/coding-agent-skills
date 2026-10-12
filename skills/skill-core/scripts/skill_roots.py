"""Where skills live: a sibling skill's scripts, and the skills directories a script must not write inside.

A script reaches a skill_deps sibling's modules through `sibling_scripts`, after the one line that puts skill-core on
sys.path, so the layout is known here once, in the checkout and in the deployed home alike.

A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so a
script refuses an explicit output path inside its own skills directory or one the deployer owns.
"""

from __future__ import annotations

from pathlib import Path

# The skills directory holding skill-core, and so every skill that imports it: the source tree's skills/, or the
# deployed ~/.claude/skills.
SKILLS_ROOT = Path(__file__).resolve().parents[2]


class MissingSibling(SystemExit):
    """A sibling skill a script reaches is not beside it.

    `sibling_scripts` runs while a script imports, before `console.run_main` can turn an exception into a FAILED
    line, so the lookup ends the script as a usage error does, with its message on stderr and exit code 1, rather
    than with a traceback.
    """

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


def sibling_scripts(name: str) -> Path:
    """The scripts directory of the sibling skill `name`, for a script to put on sys.path.

    A deployed home and an uncategorized checkout hold the sibling at SKILLS_ROOT/<name>; a checkout may also hold
    it in a category, at SKILLS_ROOT/<category>/<name>, which the deployer flattens on install.
    """
    direct = SKILLS_ROOT / name / "scripts"
    if direct.is_dir():
        return direct
    categorized = sorted(
        skill / "scripts"
        for skill in SKILLS_ROOT.glob(f"*/{name}")
        if (skill / "SKILL.md").is_file() and (skill / "scripts").is_dir()
    )
    if len(categorized) == 1:
        return categorized[0]
    if categorized:
        found = ", ".join(str(path) for path in categorized)
        raise MissingSibling(name, f"skill {name} is in more than one category under {SKILLS_ROOT}: {found}")
    raise MissingSibling(
        name, f"skill {name} has no scripts under {SKILLS_ROOT}; deploy the skills that declare it in skill_deps again"
    )


def deployed_skill_roots() -> tuple[Path, ...]:
    """The directories the deployer owns, whatever skills directory this script runs from."""
    home = Path.home()
    return home / ".claude" / "skills", home / ".agents" / "skills"


def skills_directory_holding(path: Path) -> Path | None:
    """The skills directory `path` lies inside, the running skills' own or one the deployer owns, or None."""
    target = path.resolve()
    return next(
        (root for root in (SKILLS_ROOT, *deployed_skill_roots()) if target.is_relative_to(root.resolve())), None
    )

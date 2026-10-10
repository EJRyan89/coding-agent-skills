"""Where skills live, for scripts that must not write inside a skill directory.

A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so a
script refuses an explicit output path inside its own skills directory or one the deployer owns.
"""

from __future__ import annotations

from pathlib import Path

# The skills directory holding skill-core, and so every skill that imports it: the source tree's skills/, or the
# deployed ~/.claude/skills.
SKILLS_ROOT = Path(__file__).resolve().parents[2]


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

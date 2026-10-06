"""Where the deployer installs skills, for scripts that must not write inside a skill directory.

A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so a
script refuses an explicit output path inside its own skills directory or one of these.
"""

from __future__ import annotations

from pathlib import Path


def deployed_skill_roots() -> tuple[Path, ...]:
    """The directories the deployer owns, whatever skills directory this script runs from."""
    home = Path.home()
    return home / ".claude" / "skills", home / ".agents" / "skills"

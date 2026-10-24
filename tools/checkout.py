"""The one check that a directory a repository tool is pointed at is a checkout of this repository.

tools/new_skill.py, tools/plugin_release.py, tools/runtime_prompts.py, tools/skill_reference.py, and
tools/skill_shims.py write into the checkout they run from, and take --root to work on another instead, as their
regression suites do. A grant over one of them that ends in * covers
--root too, so each parses it with checkout_root, which refuses any other directory. A checkout holds a source.json
file and a .claude/skills directory, and neither it nor its .claude/skills or .agents/skills lies in a skills directory
ejryan89-skill-core's skill_roots names: the checkout's own skills/, or ~/.claude/skills and ~/.agents/skills, which the
deployer owns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import skills_directory_holding

ROOT_HELP = (
    "work on this checkout of the repository instead: a directory holding source.json and .claude/skills, outside "
    "every skills directory"
)


def checkout_problem(root: Path) -> str | None:
    """Why root is not a checkout of this repository a tool may write into, or None when it is one."""
    skills = root / ".claude" / "skills"
    if not (root / "source.json").is_file() or not skills.is_dir():
        return (
            f"{root} is not a checkout of this repository: it needs a source.json file and a .claude/skills directory"
        )
    for path in (root, skills, root / ".agents" / "skills"):
        holder = skills_directory_holding(path)
        if holder is not None:
            return f"{path} lies in the skills directory {holder}, so {root} is not a checkout to write into"
    return None


def checkout_root(value: str) -> Path:
    """The --root directory, for argparse's type: a usage error names why a directory is not a checkout."""
    root = Path(value)
    problem = checkout_problem(root)
    if problem is not None:
        raise argparse.ArgumentTypeError(problem)
    return root

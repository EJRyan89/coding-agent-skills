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
# The prefixes of Windows' extended-length spellings: `\\?\C:\x` names `C:\x`, and `\\?\UNC\host\share\x` names
# `\\host\share\x`. `Path.resolve` keeps either, and gives a volume's GUID path or a `\\.\` path back with the first,
# so a path and a root are compared without them. pathlib reads each as part of a path's drive, which a path has only
# on Windows, so elsewhere the same characters are an ordinary name and are kept.
EXTENDED_PREFIX = "\\\\?\\"
EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


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

    The skills directory is flat in the checkout and the deployed home alike, so the sibling is SKILLS_ROOT/<name>,
    beside skill-core.
    """
    scripts = SKILLS_ROOT / name / "scripts"
    if scripts.is_dir():
        return scripts
    raise MissingSibling(
        name, f"skill {name} has no scripts under {SKILLS_ROOT}; deploy the skills that declare it in skill_deps again"
    )


def deployed_skill_roots() -> tuple[Path, ...]:
    """The directories the deployer owns, whatever skills directory this script runs from."""
    home = Path.home()
    return home / ".claude" / "skills", home / ".agents" / "skills"


def _without_extended_prefix(path: Path) -> Path:
    """`path` without Windows' extended-length prefix, when one comes before a drive letter or a UNC share; any other
    path, such as a volume's GUID path that no drive letter names, is kept as it is."""
    if not path.drive:
        return path
    text = str(path)
    if text[: len(EXTENDED_UNC_PREFIX)].upper() == EXTENDED_UNC_PREFIX:
        plain = Path("\\\\" + text[len(EXTENDED_UNC_PREFIX) :])
    elif text.startswith(EXTENDED_PREFIX):
        plain = Path(text[len(EXTENDED_PREFIX) :])
    else:
        return path
    return plain if plain.drive else path


def skills_directory_holding(path: Path) -> Path | None:
    """The skills directory `path` lies inside, the running skills' own or one the deployer owns, or None. Each is
    compared resolved and without an extended-length prefix, so every spelling of a path inside one is found."""
    target = _without_extended_prefix(path.resolve())
    return next(
        (
            root
            for root in (SKILLS_ROOT, *deployed_skill_roots())
            if target.is_relative_to(_without_extended_prefix(root.resolve()))
        ),
        None,
    )

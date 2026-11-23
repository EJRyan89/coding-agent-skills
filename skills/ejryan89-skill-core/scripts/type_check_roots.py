"""The roots validation type-checks with mypy, which of them run in strict mode, and the name of each type-check job.

mypy checks the roots in TYPE_CHECK_ROOTS together from the repository root, in one run at the configured strictness,
and each of those that `[tool.validation] mypy-strict` in `pyproject.toml` lists again alone in strict mode; it checks
each skill's `scripts/` that holds a module from inside it, in a run of its own, strict when the list names it.
tests/validation/python_checks.py builds its jobs from these, and the implement-change repository skill's script
names, from the same reading, the jobs that check the files a change touches, so the command it prints runs the jobs
continuous integration runs. Every function is given the repository's root or its `pyproject.toml`, and finds neither
itself.
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection, Iterable
from pathlib import Path

import file_presence
from path_containment import within

# The roots mypy checks together from the repository root; each skill's scripts/ is checked from inside it, where the
# deployed skill's own imports resolve.
TYPE_CHECK_ROOTS = ("deployer", "tools", "deploy.py", "tests", "conftest.py")
# The repository's own skills, which validation holds as it holds a shipped skill under skills/.
REPOSITORY_SKILLS = ".claude/skills"
# The configuration file at the repository root whose [tool.validation] mypy-strict lists the strict roots.
PYPROJECT = "pyproject.toml"


def skill_directories(root: Path) -> list[Path]:
    """Each shipped skill's directory under `root`: skills/<name>, holding SKILL.md."""
    skills = root / "skills"
    found = (path.parent for path in file_presence.matching(skills, "*/SKILL.md"))
    return sorted(found, key=lambda path: path.relative_to(skills).as_posix().casefold())


def repository_skill_directories(root: Path) -> list[Path]:
    """Each repository skill's directory under `root`'s .claude/skills."""
    return sorted(
        (path.parent for path in file_presence.matching(root / REPOSITORY_SKILLS, "*/SKILL.md")),
        key=lambda path: path.name,
    )


def all_skill_directories(root: Path) -> list[Path]:
    """Every shipped and repository skill's directory: validation finds, lays out, lints, and types their scripts/."""
    return [*skill_directories(root), *repository_skill_directories(root)]


def skill_script_directories(root: Path) -> list[Path]:
    """The scripts/ directory of every shipped and repository skill that has one."""
    return [
        skill / "scripts"
        for skill in all_skill_directories(root)
        if file_presence.is_directory(skill / "scripts", other_is_absent=True)
    ]


def type_check_skill_roots(root: Path) -> list[Path]:
    """The scripts directory of each shipped or repository skill that holds a Python module or regression suite."""
    return [scripts for scripts in skill_script_directories(root) if any(file_presence.matching(scripts, "*.py"))]


def strict_type_check_areas(pyproject: Path) -> list[str] | None:
    """The areas `pyproject`'s [tool.validation] mypy-strict lists, or None when it holds no list of paths. A file
    that cannot be read, or is not UTF-8 TOML, raises OSError, UnicodeDecodeError, or tomllib's TOMLDecodeError."""
    configured = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    areas = configured.get("tool", {}).get("validation", {}).get("mypy-strict")
    if not isinstance(areas, list) or not all(isinstance(area, str) for area in areas):
        return None
    return areas


def type_check_root(root: Path, path: Path, skill_scripts: Iterable[Path]) -> Path | None:
    """The folder mypy collects a file from: its skill's scripts/, or the root of TYPE_CHECK_ROOTS holding it, or None
    for a file no type check reads from where it sits. `path` and the folders are absolute, under `root`."""
    for scripts in skill_scripts:
        if within(path, scripts):
            return scripts
    for top in TYPE_CHECK_ROOTS:
        if within(path, root / top):
            return root / top
    return None


def shared_job_name() -> str:
    """The job that checks TYPE_CHECK_ROOTS together, at the configured strictness."""
    return f"static type check (mypy {', '.join(TYPE_CHECK_ROOTS)})"


def strict_job_name(top: str) -> str:
    """The job that checks one of TYPE_CHECK_ROOTS alone in strict mode."""
    return f"static type check (mypy --strict {top})"


def skill_job_name(scripts: str) -> str:
    """The job that checks a skill's scripts/, named by its path from the repository root, strict or not, so the
    seconds recorded for it still weigh it."""
    return f"static type check (mypy {scripts})"


def file_type_check_jobs(root: Path, paths: Iterable[str], strict: Collection[str]) -> list[str]:
    """The name of each type-check job that reads one of the Python files `paths` names, as paths from `root` with `/`
    between their parts, once, in the order the files first reach it: the shared run and, when `strict` lists it, the
    strict run of a root of TYPE_CHECK_ROOTS, or a skill's own run, for a skill whose scripts/ holds a module."""
    skill_roots = type_check_skill_roots(root)
    jobs: dict[str, None] = {}
    for name in paths:
        if not name.endswith(".py"):
            continue
        checked_from = type_check_root(root, root.joinpath(*name.split("/")), skill_roots)
        if checked_from is None:
            continue
        relative = checked_from.relative_to(root).as_posix()
        if checked_from in skill_roots:
            jobs[skill_job_name(relative)] = None
            continue
        jobs[shared_job_name()] = None
        if relative in strict:
            jobs[strict_job_name(relative)] = None
    return list(jobs)

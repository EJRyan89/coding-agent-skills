"""What every policy module shares: the repository's roots, how a regression suite and a skill script are
recognized, the Markdown fence and section readers, and the fixture-tree writer.

The modules of tests/validation import the deployer, so whatever imports them first puts the repository root on
sys.path: tests/run_validation.py, or a fixture suite beside them.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from deployer import render

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SKILLS_ROOT = REPOSITORY_ROOT / "skills"
REPOSITORY_SKILLS = ".claude/skills"
# A skill's executable files are Bash, Python, and PowerShell, each run by a standard command in deployer/tools.py.
SCRIPT_INTERPRETERS = {".bash": "bash", ".sh": "bash", ".py": "python", ".ps1": "pwsh"}
EXECUTABLE_SCRIPT_EXTENSIONS = set(SCRIPT_INTERPRETERS)
# No runtime these need is a deployment prerequisite and the renderer cannot validate them, so a skill holds none.
UNSUPPORTED_SCRIPT_EXTENSIONS = {".cjs", ".js", ".mjs", ".ts"}
TEST_SCRIPT_EXTENSIONS = {".py", ".sh", ".ps1"}
# A regression suite is found by its name alone, matched against the file's stem or its whole name, ignoring case.
TEST_NAME_PATTERNS = ("test_*", "test-*", "*_test", "*-test", "*.test.*")
# Every suite runs as `python <file>`, so a Python suite without this entry point runs no tests and still exits 0.
PYTHON_ENTRY_POINT = 'if __name__ == "__main__":'
SKILL_GUIDE = "docs/adding-a-skill.md"
TEMPLATE_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
SHELL_FENCES = {"bash", "sh", "shell"}


def fence_holders(lines: list[str]) -> list[render.Fence | None]:
    """For each line, the fence holding it as its opener, body, or closer, or None outside every fence.

    Every Markdown policy here reads fences through render.find_fences, the detector the deployer renders with.
    """
    holders: list[render.Fence | None] = [None] * len(lines)
    for fence in render.find_fences(lines):
        for index in range(fence.opener, fence.closer + 1 if fence.closer is not None else len(lines)):
            holders[index] = fence
    return holders


def _fence_body_line(holder: render.Fence | None, index: int) -> bool:
    return holder is not None and index not in (holder.opener, holder.closer)


def repository_files(root: Path) -> list[Path]:
    """Tracked files plus untracked ones Git does not ignore, which is what a commit could include."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [root / name for name in listed.split("\0") if name and (root / name).is_file()]


def scripts_put_on_path(source: str) -> list[str]:
    """The skill names in each `sys.path.insert(..., ... / "<skill>" / "scripts")` call in the source."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "insert"
            and ast.unparse(node.func.value) == "sys.path"
        ):
            continue
        for part in ast.walk(node):
            if (
                isinstance(part, ast.BinOp)
                and isinstance(part.op, ast.Div)
                and isinstance(part.right, ast.Constant)
                and part.right.value == "scripts"
                and isinstance(part.left, ast.BinOp)
                and isinstance(part.left.right, ast.Constant)
                and isinstance(part.left.right.value, str)
            ):
                names.append(part.left.right.value)
    return names


def _markdown_section(text: str, heading: str) -> str | None:
    lines = text.split("\n")
    if heading not in lines:
        return None
    start = lines.index(heading) + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def is_executable_script(path: Path) -> bool:
    return path.suffix.casefold() in EXECUTABLE_SCRIPT_EXTENSIONS


def is_shell_script(path: Path) -> bool:
    return path.suffix.casefold() in {".sh", ".bash"}


def is_test_script(path: Path) -> bool:
    if path.suffix.casefold() not in TEST_SCRIPT_EXTENSIONS:
        return False
    names = (path.stem.casefold(), path.name.casefold())
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in TEST_NAME_PATTERNS for name in names)


def relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Each shipped skill's directory, found as deployer/source.py finds it: skills/<name> or skills/<category>/<name>,
    holding SKILL.md. skill_tree_problems reports any other directory under skills/."""
    skills = root / "skills"
    found = (path.parent for path in skills.rglob("SKILL.md") if len(path.relative_to(skills).parts) in (2, 3))
    return sorted(found, key=lambda path: path.relative_to(skills).as_posix().casefold())


def repository_skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Each repository skill's directory under .claude/skills, which validation holds as it holds a shipped skill."""
    return sorted((path.parent for path in (root / REPOSITORY_SKILLS).glob("*/SKILL.md")), key=lambda path: path.name)


def all_skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Every shipped and repository skill's directory: validation finds, lays out, lints, and types their scripts/."""
    return [*skill_directories(root), *repository_skill_directories(root)]


def skill_script_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """The scripts/ directory of every shipped and repository skill that has one."""
    return [skill / "scripts" for skill in all_skill_directories(root) if (skill / "scripts").is_dir()]


def write_fixture_tree(root: Path, files: Mapping[str, str]) -> None:
    """Write each relative path's literal text under root, creating its folders."""
    for relative_path, text in files.items():
        (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (root / relative_path).write_text(text, encoding="utf-8")

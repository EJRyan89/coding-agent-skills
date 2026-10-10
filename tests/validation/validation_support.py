"""What every policy module shares: the repository's roots, how a regression suite and a skill script are
recognized, the reader of a module's imports, the Markdown fence and section readers, and the fixture-tree writer.

The modules of tests/validation import the deployer, so whatever imports them first puts the repository root on
sys.path: tests/run_validation.py, or a fixture suite beside them.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath

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


@dataclass(frozen=True)
class ModuleImport:
    """One module an import statement imports, and what it binds each name to: `import a.b as c` imports a.b and
    binds c to a.b, `import a.b` binds a to a, and `from a import b as c` imports a and binds c to a.b."""

    line: int
    module: str
    bindings: tuple[tuple[str, str], ...]


def module_imports(tree: ast.AST) -> list[ModuleImport]:
    """Every absolute import anywhere in a module, in the order ast.walk meets it. Every policy that reads what a
    module imports reads it here; a relative import names no module these policies can find, so it is left out."""
    found: list[ModuleImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                binding = (alias.asname, alias.name) if alias.asname else (top, top)
                found.append(ModuleImport(node.lineno, alias.name, (binding,)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bindings = tuple((alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names)
            found.append(ModuleImport(node.lineno, node.module, bindings))
    return found


def import_aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a module binds by import stands for, anywhere in it: `import shutil as sh` binds sh to shutil,
    `from tempfile import TemporaryFile as T` binds T to tempfile.TemporaryFile, and `import os.path` binds os."""
    return {name: target for found in module_imports(tree) for name, target in found.bindings}


def qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    """The dotted name an expression stands for through the module's imports, such as shutil.rmtree for sh.rmtree
    after `import shutil as sh`, or "" when it is not a dotted name. A name bound otherwise stands for itself."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else ""
    return ""


def fence_holders(lines: list[str]) -> list[render.Fence | None]:
    """For each line, the fence holding it as its opener, body, or closer, or None outside every fence.

    Every Markdown policy here reads fences through render.find_fences, the detector the deployer renders with.
    """
    holders: list[render.Fence | None] = [None] * len(lines)
    for fence in render.find_fences(lines):
        for index in range(fence.opener, fence.closer + 1 if fence.closer is not None else len(lines)):
            holders[index] = fence
    return holders


def fence_body_line(holder: render.Fence | None, index: int) -> bool:
    """Whether the line at index is inside the fence that holds it, rather than its opener or closer."""
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


def markdown_section(text: str, heading: str) -> str | None:
    """The lines after the heading line up to the next second-level heading, or None when the heading is absent."""
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


def is_skill_file(relative_path: PurePath) -> bool:
    """Whether a path relative to the repository is a shipped skill's SKILL.md, found as deployer/source.py finds it:
    skills/<name>/SKILL.md or skills/<category>/<name>/SKILL.md."""
    parts = relative_path.parts
    return len(parts) in (3, 4) and parts[0] == "skills" and parts[-1] == "SKILL.md"


def skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Each shipped skill's directory: skills/<name> or skills/<category>/<name>, holding SKILL.md.
    skill_tree_problems reports any other directory under skills/."""
    skills = root / "skills"
    found = (path.parent for path in skills.rglob("SKILL.md") if is_skill_file(path.relative_to(root)))
    return sorted(found, key=lambda path: path.relative_to(skills).as_posix().casefold())


def skills_by_name(root: Path = REPOSITORY_ROOT) -> dict[str, Path]:
    """Each shipped skill's directory by its name, a skill in a category included: the one lookup every policy that
    starts from a skill's name, in deploy-meta or skill_deps, uses to find its files."""
    return {path.name: path for path in skill_directories(root)}


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


def module_allowance(tree: ast.Module, name: str) -> dict[str, str] | None:
    """A module's allowance, such as PLATFORM_ALLOWED, or None when it does not map each token to its reason.

    Every policy that lets a module sanction an exception beside the code it excuses reads the allowance here, so
    they agree on its shape: a module-level literal dictionary of non-empty reasons, empty when the module has none.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            try:
                allowance = ast.literal_eval(node.value)
            except ValueError:
                return None
            valid = isinstance(allowance, dict) and all(
                isinstance(token, str) and isinstance(reason, str) and reason.strip()
                for token, reason in allowance.items()
            )
            return allowance if valid else None
    return {}


def docstring_ids(tree: ast.Module) -> set[int]:
    """The id of each docstring's node: the module's, and each class's and function's."""
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

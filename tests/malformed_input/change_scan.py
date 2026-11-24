"""Every function in a shipped skill's scripts that changes a file, found by reading the code, so
test_change_places.py can fail one that no row of its table holds.

A function changes a file when it calls one of the change steps, as an attribute or by the name it is imported as:
ejryan89-skill-core's `replace_bytes` and `replace_text`, `json_writer.write`, code-review-core's `atomic_write_bytes`,
`atomic_write_text`, and `atomic_write_json`, and the standard library's `unlink`, `rename`, `rmtree`, `rmdir`,
`write_text`, `write_bytes`, `os.remove`, `os.replace`, and `shutil.move`. A step that makes a file only where nothing
is, such as `create_bytes` or a `mkdir`, changes none, so it is no step here. A function is named as reader_scan.py
names a reader: a method by `Class.method`, a function nested in another by the outermost function, and the module by
its name alone, as a skill's scripts put one another on `sys.path`. Only the scripts under skills/ are read, the
suites aside, and atomic_file, the change step itself.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from reader_scan import REPOSITORY

# A step called by its name or as an attribute of anything: no other kind of object has a method of the name.
STEPS = frozenset(
    {
        "replace_bytes",
        "replace_text",
        "atomic_write_bytes",
        "atomic_write_text",
        "atomic_write_json",
        "unlink",
        "rename",
        "rmtree",
        "rmdir",
        "write_text",
        "write_bytes",
    }
)
# A step called as an attribute of the module named, since another object has a method of the name: str.replace,
# list.remove, and a stream's write.
MODULE_STEPS = {
    "os": frozenset({"remove", "replace"}),
    "shutil": frozenset({"move"}),
    "json_writer": frozenset({"write"}),
}
# The change step itself, whose callers are the places.
STEP_MODULES = frozenset({"atomic_file"})


def skill_modules(root: Path = REPOSITORY) -> Iterator[tuple[str, Path]]:
    """Each module of a shipped skill's scripts the scan reads, by its name: the suites and the change step aside."""
    for path in sorted(root.glob("skills/*/scripts/*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py" or path.stem in STEP_MODULES:
            continue
        yield path.stem, path


def _changes(node: ast.AST) -> bool:
    """Whether anything below node calls a change step."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        function = call.func
        if isinstance(function, ast.Name) and function.id in STEPS:
            return True
        if isinstance(function, ast.Attribute):
            if function.attr in STEPS:
                return True
            owner = function.value
            if isinstance(owner, ast.Name) and function.attr in MODULE_STEPS.get(owner.id, frozenset()):
                return True
    return False


def changing_places(module: str, text: str) -> list[str]:
    """The functions of one module's source that call a change step, each named as a table row names it."""
    found: list[str] = []
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _changes(node):
            found.append(f"{module}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            found += [
                f"{module}.{node.name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and _changes(method)
            ]
    return found


def skill_places(root: Path = REPOSITORY) -> list[str]:
    """Every function of a shipped skill's scripts that changes a file."""
    return [name for module, path in skill_modules(root) for name in changing_places(module, path.read_text("utf-8"))]

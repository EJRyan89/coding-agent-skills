"""Every function in a shipped skill's scripts that changes a file, found by reading the code, so
test_change_places.py can fail one that no row of its table holds.

A call is read through ast, its name resolved through the module's imports by ejryan89-skill-core's python_names, the
one reader of what a module's names stand for, and through the module's own functions, which stand for
`<module>.<name>`. A call changes a file when its name resolves to one of QUALIFIED_STEPS, such as
`atomic_file.replace_text` imported under any name, `os.replace`, or `shutil.rmtree`; when it calls a path's method of
METHOD_STEPS, such as `unlink` or `write_text`, on anything but a name a module is imported as; or when it calls
`.replace` with one argument and no keyword, as a path's rename does, where `str.replace` takes two or more. A step
that makes a file only where nothing is, such as `atomic_file.create_bytes` or a `mkdir`, changes none, so it is no
step here. A function is named as reader_scan.py names a reader: a method by `Class.method`, a function nested in
another by the outermost function, and the module by its name alone, as a skill's scripts put one another on
`sys.path`. Only the scripts under skills/ are read, the suites aside, and atomic_file, the change step itself.

Its stated limits, each shown by test_change_places.py's fixture: a step handed on as a value, as `map(os.remove,
paths)`, is not a call and is not read; `list.remove` and a stream's `write` are no steps, so a path's `remove` method,
which no path has, and a file object's `write` are not read; and a call through a name bound otherwise, as `remove =
os.remove`, resolves to the name and is not read.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from reader_scan import REPOSITORY

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from python_names import import_aliases, qualified_name

# Each change step by the dotted name a call resolves to.
QUALIFIED_STEPS = frozenset(
    {
        "atomic_file.replace_bytes",
        "atomic_file.replace_text",
        "json_writer.write",
        "review_io.atomic_write_bytes",
        "review_io.atomic_write_text",
        "review_io.atomic_write_json",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.truncate",
        "shutil.rmtree",
        "shutil.move",
    }
)
# A path's methods that change a file, called on anything but a module.
METHOD_STEPS = frozenset({"unlink", "rename", "rmdir", "write_text", "write_bytes"})
# The change step itself, whose callers are the places.
STEP_MODULES = frozenset({"atomic_file"})


def skill_modules(root: Path = REPOSITORY) -> Iterator[tuple[str, Path]]:
    """Each module of a shipped skill's scripts the scan reads, by its name: the suites and the change step aside."""
    for path in sorted(root.glob("skills/*/scripts/*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py" or path.stem in STEP_MODULES:
            continue
        yield path.stem, path


def is_step(call: ast.Call, names: dict[str, str]) -> bool:
    """Whether a call changes a file, its name resolved through `names`: the module's imports and its own functions."""
    if qualified_name(call.func, names) in QUALIFIED_STEPS:
        return True
    function = call.func
    if not isinstance(function, ast.Attribute):
        return False
    owner = function.value
    if isinstance(owner, ast.Name) and owner.id in names and "." not in names[owner.id]:
        return False  # a module's function, which QUALIFIED_STEPS alone names
    if function.attr in METHOD_STEPS:
        return True
    return function.attr == "replace" and len(call.args) == 1 and not call.keywords


def _changes(node: ast.AST, names: dict[str, str]) -> bool:
    """Whether anything below node calls a change step."""
    return any(isinstance(call, ast.Call) and is_step(call, names) for call in ast.walk(node))


def changing_places(module: str, text: str) -> list[str]:
    """The functions of one module's source that call a change step, each named as a table row names it."""
    tree = ast.parse(text)
    names = {
        **{
            node.name: f"{module}.{node.name}"
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        },
        **import_aliases(tree),
    }
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _changes(node, names):
            found.append(f"{module}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            found += [
                f"{module}.{node.name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and _changes(method, names)
            ]
    return found


def skill_places(root: Path = REPOSITORY) -> list[str]:
    """Every function of a shipped skill's scripts that changes a file."""
    return [name for module, path in skill_modules(root) for name in changing_places(module, path.read_text("utf-8"))]

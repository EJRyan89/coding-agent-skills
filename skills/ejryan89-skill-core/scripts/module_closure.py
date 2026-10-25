"""The ejryan89-skill-core modules a module runs with: itself and every module of the same `scripts` directory it
imports, and theirs in turn, read through `ast`.

A fixture that builds a throwaway copy of these modules, such as a skills tree a test deploys or runs a script in,
copies what `copy_with_imports` derives from the source, never a list a contributor keeps: a list is one module short
as soon as a copied module imports another, and the fixture's script then fails with `ModuleNotFoundError` in a suite
the change never touched. A test that leaves a module out on purpose removes it from the built tree by name.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

# This module's own directory: ejryan89-skill-core's scripts, in the checkout or wherever it was deployed.
SCRIPTS = Path(__file__).resolve().parent
# The calls that import a module named by a string, whose literal argument counts as an import.
MODULE_LOADERS = frozenset({"import_module", "__import__"})


def imported_names(source: str) -> set[str]:
    """The top-level name of every module `source` imports: by a statement, wherever it stands, or by an
    import_module or __import__ call given one string literal."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if function in MODULE_LOADERS and isinstance(node.args[0].value, str):
                names.add(node.args[0].value.split(".")[0])
    return names


def imports_of(path: Path, scripts: Path = SCRIPTS) -> list[str]:
    """The modules of `scripts` the file at `path`, such as another skill's script, imports directly, sorted."""
    names = imported_names(path.read_text(encoding="utf-8"))
    return sorted(name for name in names if (scripts / f"{name}.py").is_file())


def closure(*modules: str, scripts: Path = SCRIPTS) -> list[str]:
    """The named modules of `scripts` and every module of it they import, directly or through another, by name and
    sorted. A named module that is not there raises FileNotFoundError."""
    found: set[str] = set()
    pending = list(modules)
    while pending:
        module = pending.pop()
        if module in found:
            continue
        found.add(module)
        source = (scripts / f"{module}.py").read_text(encoding="utf-8")
        pending += [name for name in imported_names(source) if (scripts / f"{name}.py").is_file()]
    return sorted(found)


def copy_with_imports(destination: Path, *modules: str, scripts: Path = SCRIPTS) -> list[str]:
    """Copy the named modules of `scripts`, with every module of it they import, into `destination`, which is
    created. Returns the names of the modules copied."""
    copied = closure(*modules, scripts=scripts)
    destination.mkdir(parents=True, exist_ok=True)
    for module in copied:
        shutil.copyfile(scripts / f"{module}.py", destination / f"{module}.py")
    return copied

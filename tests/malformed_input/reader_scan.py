"""Every function in the repository's code and validation's policies that reads a record through one of the shared
readers, found by reading the code, so test_malformed_input.py can fail one that no reader table holds and no entry
names as owed or as no reader.

A call counts when it names a reader of ejryan89-skill-core's on its module, as `json_reader.loads(...)`, or by a name
imported from it, as `from json_reader import loads`: `json_reader.loads`, `json_reader.raw_decode`, and
`json_reader.read_file`; `frontmatter.read`, `frontmatter.parse`, `frontmatter.split`, and `frontmatter.Frontmatter`;
and `xml_reader.read_xml`. A call is the function's that holds it: a method by `Class.method`, and a function nested in
another by the outermost function, since that is the reader a table calls. A function is named as its table entry names
it: `tools.<module>.<function>` under tools/, `deployer.<module>.<function>` under deployer/, and `<module>.<function>`
under a skill's or a repository skill's scripts/ or tests/validation/, whose folder its callers put on `sys.path`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]

# Each shared reader by its module, with the names that read: a module's other names, such as its error, read nothing.
SHARED_READERS = {
    "json_reader": frozenset({"loads", "raw_decode", "read_file"}),
    "frontmatter": frozenset({"read", "parse", "split", "Frontmatter"}),
    "xml_reader": frozenset({"read_xml"}),
}


def code_files(root: Path = REPOSITORY) -> Iterator[tuple[str, Path]]:
    """Each module the scan reads, with the prefix its functions are named by: tools/ and deployer/, and each skill's
    and repository skill's scripts/, their suites and the shared readers themselves aside."""
    folders = [
        ("tools.", root / "tools"),
        ("deployer.", root / "deployer"),
        *(("", folder) for folder in sorted(root.glob("skills/*/scripts"))),
        *(("", folder) for folder in sorted(root.glob(".claude/skills/*/scripts"))),
        # Validation's policies read the repository's tables and records too, such as the known-failure list.
        ("", root / "tests" / "validation"),
    ]
    for prefix, folder in folders:
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("test_") or path.name == "conftest.py" or path.stem in SHARED_READERS:
                continue
            yield f"{prefix}{path.stem}", path


def reader_names(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """The names a module binds to a shared reader module, each with the module it binds, and the names it binds to
    one of their reading names."""
    modules: dict[str, str] = {}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name: alias.name for alias in node.names if alias.name in SHARED_READERS}
        elif isinstance(node, ast.ImportFrom) and node.module in SHARED_READERS and node.level == 0:
            functions |= {
                alias.asname or alias.name for alias in node.names if alias.name in SHARED_READERS[node.module]
            }
    return modules, functions


def calls_a_reader(node: ast.AST, modules: dict[str, str], functions: set[str]) -> bool:
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        called = call.func
        if isinstance(called, ast.Name) and called.id in functions:
            return True
        if (
            isinstance(called, ast.Attribute)
            and isinstance(called.value, ast.Name)
            and called.value.id in modules
            and called.attr in SHARED_READERS[modules[called.value.id]]
        ):
            return True
    return False


def reading_functions(module: str, text: str) -> list[str]:
    """The functions of one module's source that call a shared reader, each named as a table entry names it."""
    tree = ast.parse(text)
    modules, functions = reader_names(tree)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and calls_a_reader(node, modules, functions):
            found.append(f"{module}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            found += [
                f"{module}.{node.name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and calls_a_reader(method, modules, functions)
            ]
    return found


def _definitions(tree: ast.Module) -> dict[str, ast.AST]:
    """A module's functions and methods by the name reading_functions gives them, without the module."""
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[f"{node.name}.{method.name}"] = method
    return found


def _tables(tree: ast.Module, names: set[str]) -> dict[str, set[str]]:
    """Each module-level name bound to a value that names the module's own functions, such as a table of parsers by
    file name, with the functions it names."""
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        named = {each.id for each in ast.walk(value) if isinstance(each, ast.Name) and each.id in names}
        found |= {target.id: named for target in targets if isinstance(target, ast.Name) and named}
    return found


def _called(node: ast.AST, names: set[str], tables: dict[str, set[str]]) -> set[str]:
    """The module's own functions and methods `node` calls or passes on, by bare name, as `self.method`, or through a
    module-level table that names them."""
    found: set[str] = set()
    for each in ast.walk(node):
        if isinstance(each, ast.Name):
            found |= ({each.id} & names) | tables.get(each.id, set())
        elif isinstance(each, ast.Attribute) and isinstance(each.value, ast.Name) and each.value.id in {"self", "cls"}:
            found |= {name for name in names if name.endswith(f".{each.attr}")}
    return found


def reached(module: str, text: str, held: set[str]) -> set[str]:
    """The functions of one module a held reader of the same module reaches by calling its own functions, directly or
    through a module-level table of them, held readers included: a helper such a reader calls is read through that
    reader's table entry."""
    tree = ast.parse(text)
    definitions = _definitions(tree)
    names = set(definitions)
    tables = _tables(tree, names)
    seen = {name for name in names if f"{module}.{name}" in held}
    queue = list(seen)
    while queue:
        for name in _called(definitions[queue.pop()], names, tables) - seen:
            seen.add(name)
            queue.append(name)
    return {f"{module}.{name}" for name in seen}


def unheld_readers(held: set[str], root: Path = REPOSITORY) -> list[str]:
    """Every function of the repository's code that calls a shared reader and that no name in `held` is, or reaches
    from its own module."""
    unheld: list[str] = []
    for module, path in code_files(root):
        text = path.read_text(encoding="utf-8")
        covered = reached(module, text, held)
        unheld += [name for name in reading_functions(module, text) if name not in covered]
    return unheld


def repository_readers(root: Path = REPOSITORY) -> list[str]:
    """Every function of the repository's code that calls a shared reader."""
    return [
        name
        for module, path in code_files(root)
        for name in reading_functions(module, path.read_text(encoding="utf-8"))
    ]


def entry_name(name: str) -> str:
    """The function a table entry's name holds, without the label some entries add after a space or a colon."""
    return name.split(" ", 1)[0].split(":", 1)[0]

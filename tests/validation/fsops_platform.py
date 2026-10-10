"""Operating-system-specific code stays in deployer/platform_support.py, and every deployer filesystem write goes
through deployer/fsops.py.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from validation_support import REPOSITORY_ROOT, import_aliases, qualified_name

# What only deployer/platform_support.py may name, so that supporting another operating system changes one module.
PLATFORM_TOKENS: dict[str, tuple[str, ...]] = {
    "qualified": ("sys.platform", "os.name", "os.startfile"),
    "attribute": ("chmod", "fchmod", "lchmod", "st_file_attributes"),
    "keyword": ("creationflags",),
    "module": ("ctypes", "winreg", "msvcrt", "_winapi"),
    # Windows environment names, which Windows compares ignoring case, so each is matched in any case.
    "variable": ("LOCALAPPDATA", "USERPROFILE", "ProgramFiles", "APPDATA", "GIT_BASH"),
    "command": ("cygpath",),
}
# A module sanctions a use beside the code it excuses, as a module-level PLATFORM_ALLOWED = {token: reason}. An
# allowance the module no longer needs is reported.
PLATFORM_ALLOWANCE = "PLATFORM_ALLOWED"
# The module that defines PLATFORM_TOKENS, which names every token on purpose.
PLATFORM_POLICY_MODULE = "tests/validation/fsops_platform.py"


def _platform_scanned_files(root: Path) -> list[Path]:
    """The code that runs on a user's machine or runs validation, other than platform_support itself."""
    files = [root / "deploy.py", root / "tests" / "run_validation.py", root / "tests" / "run_shard.py"]
    files += [*(root / "tests" / "validation").glob("*.py")]
    files += [*(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    return [
        path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/platform_support.py"
    ]


def _platform_names(tree: ast.Module, skipped_tables: set[str]) -> list[tuple[int, str]]:
    """The platform tokens a module's code names, as (line, token), ignoring comments, docstrings, and test cases."""
    docstrings = _docstring_ids(tree)
    aliases = import_aliases(tree)
    command = re.compile(r"\b(?:" + "|".join(map(re.escape, PLATFORM_TOKENS["command"])) + r")\b")
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if _is_test_case(node):
            return  # A test names what it tests, and its fixtures name tokens on purpose.
        if _assigns_table(node, skipped_tables):
            return
        found.extend(_platform_node_names(node, docstrings, command, aliases))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _docstring_ids(tree: ast.Module) -> set[int]:
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


def _is_test_case(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and any(
        (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) == "TestCase"
        for base in node.bases
    )


def _assigns_table(node: ast.AST, skipped_tables: set[str]) -> bool:
    """Whether node assigns one of skipped_tables, by name, with or without an annotation."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id in skipped_tables for target in targets):
            return True
    return False


def _platform_node_names(
    node: ast.AST, docstrings: set[int], command: re.Pattern[str], aliases: dict[str, str]
) -> list[tuple[int, str]]:
    """The platform tokens node itself names, without its children's."""
    if isinstance(node, ast.Attribute):
        return _platform_attribute_names(node, aliases)
    if isinstance(node, ast.Import):
        return [found for alias in node.names for found in _platform_module_name(node.lineno, alias.name)]
    if isinstance(node, ast.ImportFrom):
        return _platform_import_from_names(node)
    if isinstance(node, ast.keyword) and node.arg in PLATFORM_TOKENS["keyword"]:
        return [(node.lineno, node.arg)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
        return _platform_string_names(node.lineno, node.value, command)
    return []


def _platform_module_name(line: int, module: str) -> list[tuple[int, str]]:
    top = module.split(".")[0]
    return [(line, top)] if top in PLATFORM_TOKENS["module"] else []


def _platform_attribute_names(node: ast.Attribute, aliases: dict[str, str]) -> list[tuple[int, str]]:
    qualified = qualified_name(node, aliases)
    if qualified in PLATFORM_TOKENS["qualified"]:
        return [(node.lineno, qualified)]
    if node.attr in PLATFORM_TOKENS["attribute"]:
        return [(node.lineno, node.attr)]
    return []


def _platform_import_from_names(node: ast.ImportFrom) -> list[tuple[int, str]]:
    """The module's token, or else each imported name that is a token."""
    module = node.module or ""
    found = _platform_module_name(node.lineno, module)
    if found:
        return found
    for alias in node.names:
        qualified = f"{module}.{alias.name}"
        if qualified in PLATFORM_TOKENS["qualified"]:
            found.append((node.lineno, qualified))
        elif alias.name in PLATFORM_TOKENS["attribute"]:
            found.append((node.lineno, alias.name))
    return found


def _platform_string_names(line: int, value: str, command: re.Pattern[str]) -> list[tuple[int, str]]:
    # Equality catches an environment lookup or entry, never prose that mentions the variable, and it ignores case
    # because Windows does: os.environ.get("PROGRAMFILES") reads ProgramFiles.
    variables = {variable.casefold() for variable in PLATFORM_TOKENS["variable"]}
    found = [(line, value)] if value.casefold() in variables else []
    found.extend((line, name) for name in sorted(set(command.findall(value))))
    return found


def _platform_allowance(tree: ast.Module, name: str = PLATFORM_ALLOWANCE) -> dict[str, str] | None:
    """A module's allowance, such as PLATFORM_ALLOWED, or None when it does not map each token to its reason."""
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


def platform_code_problems(root: Path) -> list[str]:
    """Report operating-system-specific code outside deployer/platform_support.py, and allowances no longer needed.

    CLAUDE.md keeps every operating-system-specific behavior in that one module, so a port changes only it.
    """
    found: set[tuple[str, int, str]] = set()
    problems: list[str] = []
    for path in sorted(_platform_scanned_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _platform_allowance(tree)
        if allowed is None:
            problems.append(f"{name}: {PLATFORM_ALLOWANCE} must map each token to the reason it is allowed")
            allowed = {}
        tables = {PLATFORM_ALLOWANCE, "PLATFORM_TOKENS"} if name == PLATFORM_POLICY_MODULE else {PLATFORM_ALLOWANCE}
        names = _platform_names(tree, tables)
        found |= {(name, line, token) for line, token in names if token not in allowed}
        problems += [
            f"{name}: {PLATFORM_ALLOWANCE} allows {token}, which it no longer names"
            for token in sorted(set(allowed) - {token for _, token in names})
        ]
    return [
        f"{name}:{line} names {token}; move it behind deployer/platform_support.py"
        for name, line, token in sorted(found)
    ] + problems


# Calls that change the filesystem. CLAUDE.md routes every one the deployer makes through deployer/fsops.py, whose
# functions tests replace to inject failures. A method named here is flagged on any object, since the policy cannot
# tell a Path from another receiver; the names are chosen so that none is a common method of anything else. Path.replace
# shares its name with str.replace, so _filesystem_writes tells them apart by their arguments instead. A qualified name
# is matched through the module's imports, so `import shutil as sh` and `from os import replace` hide no write.
FILESYSTEM_WRITES: dict[str, frozenset[str]] = {
    "method": frozenset(
        {
            "write_bytes",
            "write_text",
            "touch",
            "mkdir",
            "unlink",
            "rmdir",
            "rename",
            "chmod",
            "symlink_to",
            "hardlink_to",
        }
    ),
    "qualified": frozenset(
        {
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.chmod",
            "os.chown",
            "os.lchown",
            "os.mkdir",
            "os.makedirs",
            "os.rmdir",
            "os.removedirs",
            "os.fdopen",
            "os.link",
            "os.symlink",
            "os.truncate",
            "os.utime",
            "shutil.rmtree",
            "shutil.move",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.copytree",
            "shutil.chown",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
            "tempfile.TemporaryDirectory",
            "tempfile.NamedTemporaryFile",
            "tempfile.TemporaryFile",
            "tempfile.SpooledTemporaryFile",
        }
    ),
}
# A module sanctions a write beside the code it excuses, as a module-level FSOPS_ALLOWED = {token: reason}.
FSOPS_ALLOWANCE = "FSOPS_ALLOWED"
WRITE_MODE_CHARACTERS = "wax+"
# The functions that open a file by name with its mode second; a Path's open() takes the mode first.
OPEN_FUNCTIONS = frozenset({"open", "builtins.open", "io.open", "codecs.open"})
# The os.open flags that only read. Any other flag, a nonzero number, or flags the policy cannot read count as a write.
READ_FLAGS = frozenset({"os.O_RDONLY", "os.O_BINARY", "os.O_TEXT", "os.O_NOINHERIT", "os.O_CLOEXEC", "os.O_NOFOLLOW"})


def _writes_scanned_files(root: Path) -> list[Path]:
    """The deployer's code, other than fsops itself."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py")]
    return [path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/fsops.py"]


def _string_bindings(tree: ast.Module) -> dict[str, set[str] | None]:
    """The string constants each name is assigned anywhere in the module, or None for a name that is ever bound to
    anything else: another value, a parameter, a loop or with target, or an augmented assignment."""
    bindings: dict[str, set[str] | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            constant = node.value.value if isinstance(node.value, ast.Constant) else None
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                for name in (part for part in ast.walk(target) if isinstance(part, ast.Name)):
                    known = bindings.get(name.id, set())
                    if isinstance(target, ast.Name) and isinstance(constant, str) and known is not None:
                        bindings[name.id] = known | {constant}
                    else:
                        bindings[name.id] = None
        elif isinstance(node, ast.arg):
            bindings[node.arg] = None
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension, ast.NamedExpr, ast.withitem)):
            bound = node.optional_vars if isinstance(node, ast.withitem) else node.target
            for part in ast.walk(bound) if bound is not None else ():
                if isinstance(part, ast.Name):
                    bindings[part.id] = None
    return bindings


def _mode_argument(call: ast.Call, position: int) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((keyword.value for keyword in call.keywords if keyword.arg == "mode"), None)


def _writes_with_mode(mode: ast.expr | None, bindings: dict[str, set[str] | None]) -> bool:
    """Whether a mode may write. No mode reads, and a mode the policy cannot read, such as a parameter, writes."""
    if mode is None:
        return False
    values: set[str] | None = None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        values = {mode.value}
    elif isinstance(mode, ast.Name):
        values = bindings.get(mode.id)
    return values is None or any(character in value for value in values for character in WRITE_MODE_CHARACTERS)


def _os_open_writes(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether os.open is given flags that may write: any flag outside READ_FLAGS, a nonzero number, or flags held in
    anything but os.O_* names joined by |."""
    flags = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "flags"), None)
    if flags is None:
        return True
    pending: list[ast.expr] = [flags]
    while pending:
        part = pending.pop()
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
            pending += [part.left, part.right]
        elif isinstance(part, ast.Constant) and part.value == 0 and type(part.value) is int:
            continue
        elif qualified_name(part, aliases) not in READ_FLAGS:
            return True
    return False


def _replaces_a_path(call: ast.Call) -> bool:
    """Whether a .replace() call is Path.replace(target): one argument, where str.replace takes two."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "replace"
        and len(call.args) == 1
        and not isinstance(call.args[0], ast.Starred)
        and not call.keywords
    )


def _call_writes(call: ast.Call, aliases: dict[str, str], bindings: dict[str, set[str] | None]) -> str | None:
    """The token naming the write a call makes, or None when it makes none."""
    target = qualified_name(call.func, aliases)
    if target in FILESYSTEM_WRITES["qualified"]:
        return target
    if target == "os.open":
        return target if _os_open_writes(call, aliases) else None
    if target in OPEN_FUNCTIONS:
        return "open" if _writes_with_mode(_mode_argument(call, 1), bindings) else None
    if isinstance(call.func, ast.Attribute):
        if call.func.attr in FILESYSTEM_WRITES["method"] or _replaces_a_path(call):
            return call.func.attr
        if call.func.attr == "open" and _writes_with_mode(_mode_argument(call, 0), bindings):
            return "open"
    return None


def _write_nodes(tree: ast.Module) -> list[tuple[ast.ImportFrom | ast.Call, str]]:
    """The imports and calls through which a module's code writes to the filesystem, with each one's token, ignoring
    test cases."""
    aliases = import_aliases(tree)
    bindings = _string_bindings(tree)
    found: list[tuple[ast.ImportFrom | ast.Call, str]] = []

    def visit(node: ast.AST) -> None:
        if _is_test_case(node):
            return
        if isinstance(node, ast.ImportFrom):
            found.extend(
                (node, f"{node.module}.{alias.name}")
                for alias in node.names
                if f"{node.module}.{alias.name}" in FILESYSTEM_WRITES["qualified"]
            )
        elif isinstance(node, ast.Call) and (token := _call_writes(node, aliases, bindings)):
            found.append((node, token))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _filesystem_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """The filesystem writes a module's code makes, as (line, token), ignoring test cases."""
    return [(node.lineno, token) for node, token in _write_nodes(tree)]


TEMPORARY_DIRECTORY_CALL = "tempfile.mkdtemp"


def _in_the_temporary_directory(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a tempfile call takes keyword arguments only, none of them dir, so it writes under the system temporary
    directory. A positional argument may be dir, so none is accepted."""
    return qualified_name(call.func, aliases).startswith("tempfile.") and (
        not call.args and all(keyword.arg not in {"dir", None} for keyword in call.keywords)
    )


def _temporary_directory_names(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """The names a module binds only to a directory tempfile.mkdtemp created under the system temporary directory,
    directly or through one wrapping call such as Path(...), and never as a parameter or a loop or with target."""
    values: dict[str, list[ast.expr | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in (part for part in ast.walk(target) if isinstance(part, ast.Name)):
                    values.setdefault(name.id, []).append(node.value if isinstance(target, ast.Name) else None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.arg):
            values.setdefault(node.arg, []).append(None)
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension, ast.NamedExpr, ast.withitem)):
            rebound = node.optional_vars if isinstance(node, ast.withitem) else node.target
            for name in (part for part in ast.walk(rebound) if isinstance(part, ast.Name)) if rebound else ():
                values.setdefault(name.id, []).append(None)

    def created(value: ast.expr | None) -> bool:
        if not isinstance(value, ast.Call):
            return False
        if qualified_name(value.func, aliases) == TEMPORARY_DIRECTORY_CALL:
            return _in_the_temporary_directory(value, aliases)
        return len(value.args) == 1 and not value.keywords and created(value.args[0])

    return {name for name, bound in values.items() if all(created(value) for value in bound)}


# The path methods that take a second path, where the call writes besides its receiver.
TWO_PATH_METHODS = frozenset({"rename", "replace", "symlink_to", "hardlink_to"})


def _keyword_arguments(call: ast.Call, names: set[str]) -> list[ast.expr]:
    """The values of a call's keywords named here, and of each ** unpacking, which may hold any of them."""
    return [keyword.value for keyword in call.keywords if keyword.arg is None or keyword.arg in names]


def _write_destinations(call: ast.Call, aliases: dict[str, str]) -> list[ast.expr]:
    """The arguments that may name where a write lands. open() and os.open() write to their file and os.open() also
    relative to its dir_fd; a path method writes to its receiver and, for a rename or a link, to its argument; any
    other function may write to every argument but a constant that is not a string, such as a mode or a flag."""
    target = qualified_name(call.func, aliases)
    if target == "os.open" or target in OPEN_FUNCTIONS:
        names = {"path", "dir_fd"} if target == "os.open" else {"file"}
        return call.args[:1] + _keyword_arguments(call, names)
    if target not in FILESYSTEM_WRITES["qualified"] and isinstance(call.func, ast.Attribute):
        return [call.func.value, *(call.args if call.func.attr in TWO_PATH_METHODS else [])]
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return [
        argument
        for argument in arguments
        if not (isinstance(argument, ast.Constant) and not isinstance(argument.value, str))
    ]


def _temporary_write_problems(name: str, tree: ast.Module, allowed: dict[str, str]) -> list[str]:
    """Report each call an allowance sanctions that may write outside the system temporary directory: a tempfile call
    that is not keyword-only without dir, or any other write with a destination argument that is not a name bound only
    to a directory tempfile.mkdtemp created there. A destination built from such a name, unpacked, or held in anything
    else may be anywhere."""
    aliases = import_aliases(tree)
    directories = _temporary_directory_names(tree, aliases)
    problems: list[str] = []
    for node, token in _write_nodes(tree):
        if not isinstance(node, ast.Call) or token not in allowed:
            continue
        if token.startswith("tempfile."):
            temporary = _in_the_temporary_directory(node, aliases)
        else:
            destinations = _write_destinations(node, aliases)
            temporary = bool(destinations) and all(
                isinstance(destination, ast.Name) and destination.id in directories for destination in destinations
            )
        if not temporary:
            problems.append(
                f"{name}:{node.lineno} {FSOPS_ALLOWANCE} allows {token} only under the system temporary directory, "
                "and this call may write elsewhere"
            )
    return problems


def filesystem_write_problems(root: Path) -> list[str]:
    """Report filesystem writes in the deployer outside deployer/fsops.py, allowances no longer needed, and allowed
    writes that may land outside the system temporary directory."""
    found: set[tuple[str, int, str]] = set()
    problems: list[str] = []
    for path in sorted(_writes_scanned_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _platform_allowance(tree, FSOPS_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {FSOPS_ALLOWANCE} must map each token to the reason it is allowed")
            allowed = {}
        writes = _filesystem_writes(tree)
        found |= {(name, line, token) for line, token in writes if token not in allowed}
        problems += [
            f"{name}: {FSOPS_ALLOWANCE} allows {token}, which it no longer names"
            for token in sorted(set(allowed) - {token for _, token in writes})
        ]
        problems += _temporary_write_problems(name, tree, allowed)
    return [
        f"{name}:{line} writes with {token}; route it through deployer/fsops.py" for name, line, token in sorted(found)
    ] + problems


class FsopsPlatformPolicies(unittest.TestCase):
    def test_os_specific_code_stays_in_platform_support(self) -> None:
        # The macOS and Linux port then changes one module: deployer/platform_support.py.
        self.assertEqual([], platform_code_problems(REPOSITORY_ROOT))

    def test_deployer_filesystem_writes_go_through_fsops(self) -> None:
        # Tests inject failures by replacing deployer/fsops.py's functions, which reach only writes made through it.
        self.assertEqual([], filesystem_write_problems(REPOSITORY_ROOT))

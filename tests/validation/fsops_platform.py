"""Operating-system-specific code stays in deployer/platform_support.py, and every deployer filesystem write goes
through deployer/fsops.py.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from validation_support import REPOSITORY_ROOT

# What only deployer/platform_support.py may name, so that supporting another operating system changes one module.
PLATFORM_TOKENS: dict[str, tuple[str, ...]] = {
    "qualified": ("sys.platform", "os.name", "os.startfile"),
    "attribute": ("chmod", "fchmod", "lchmod", "st_file_attributes"),
    "keyword": ("creationflags",),
    "module": ("ctypes", "winreg", "msvcrt", "_winapi"),
    "variable": ("LOCALAPPDATA", "USERPROFILE", "ProgramFiles", "APPDATA"),
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
    command = re.compile(r"\b(?:" + "|".join(map(re.escape, PLATFORM_TOKENS["command"])) + r")\b")
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if _is_test_case(node):
            return  # A test names what it tests, and its fixtures name tokens on purpose.
        if _assigns_table(node, skipped_tables):
            return
        found.extend(_platform_node_names(node, docstrings, command))
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


def _platform_node_names(node: ast.AST, docstrings: set[int], command: re.Pattern[str]) -> list[tuple[int, str]]:
    """The platform tokens node itself names, without its children's."""
    if isinstance(node, ast.Attribute):
        return _platform_attribute_names(node)
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


def _platform_attribute_names(node: ast.Attribute) -> list[tuple[int, str]]:
    qualified = f"{node.value.id}.{node.attr}" if isinstance(node.value, ast.Name) else ""
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
    # Equality catches an environment lookup or entry, never prose that mentions the variable.
    found = [(line, value)] if value in PLATFORM_TOKENS["variable"] else []
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
# shares its name with str.replace, so _filesystem_writes tells them apart by their arguments instead.
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
            "os.mkdir",
            "os.makedirs",
            "os.rmdir",
            "os.removedirs",
            "os.fdopen",
            "os.link",
            "os.symlink",
            "shutil.rmtree",
            "shutil.move",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
            "tempfile.TemporaryDirectory",
            "tempfile.NamedTemporaryFile",
        }
    ),
}
# A module sanctions a write beside the code it excuses, as a module-level FSOPS_ALLOWED = {token: reason}.
FSOPS_ALLOWANCE = "FSOPS_ALLOWED"
WRITE_MODE_CHARACTERS = "wax+"


def _writes_scanned_files(root: Path) -> list[Path]:
    """The deployer's code, other than fsops itself."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py")]
    return [path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/fsops.py"]


def _opens_for_writing(call: ast.Call, mode_position: int) -> bool:
    """Whether open(), or a Path's open(), is given a mode that writes."""
    mode = (
        call.args[mode_position]
        if len(call.args) > mode_position
        else next((keyword.value for keyword in call.keywords if keyword.arg == "mode"), None)
    )
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(character in mode.value for character in WRITE_MODE_CHARACTERS)
    )


def _replaces_a_path(call: ast.Call) -> bool:
    """Whether a .replace() call is Path.replace(target): one argument, where str.replace takes two."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "replace"
        and len(call.args) == 1
        and not isinstance(call.args[0], ast.Starred)
        and not call.keywords
    )


def _filesystem_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """The filesystem writes a module's code makes, as (line, token), ignoring test cases."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef) and any(
            (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) == "TestCase"
            for base in node.bases
        ):
            return
        if isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if f"{node.module}.{alias.name}" in FILESYSTEM_WRITES["qualified"]
            )
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id == "open" and _opens_for_writing(node, 1):
                found.append((node.lineno, "open"))
            elif isinstance(function, ast.Attribute):
                qualified = f"{function.value.id}.{function.attr}" if isinstance(function.value, ast.Name) else ""
                if qualified in FILESYSTEM_WRITES["qualified"]:
                    found.append((node.lineno, qualified))
                elif function.attr in FILESYSTEM_WRITES["method"] or _replaces_a_path(node):
                    found.append((node.lineno, function.attr))
                elif function.attr == "open" and _opens_for_writing(node, 0):
                    found.append((node.lineno, "open"))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def filesystem_write_problems(root: Path) -> list[str]:
    """Report filesystem writes in the deployer outside deployer/fsops.py, and allowances no longer needed."""
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

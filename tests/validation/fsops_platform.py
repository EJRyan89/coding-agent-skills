"""Operating-system-specific code stays in deployer/platform_support.py, and every deployer filesystem write goes
through deployer/fsops.py.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from validation_support import (
    ATTRIBUTE_LOOKUPS,
    COMPUTED_NAME,
    MODULE_LOADERS,
    REPOSITORY_ROOT,
    CallableResolver,
    call_argument,
    call_arguments,
    docstring_ids,
    import_aliases,
    module_allowance,
    module_lookup,
    python_trees,
    qualified_name,
)

# What only deployer/platform_support.py may name, so that supporting another operating system changes one module.
PLATFORM_TOKENS: dict[str, tuple[str, ...]] = {
    "qualified": (
        "sys.platform",
        "sys.getwindowsversion",
        "sys.winver",
        "sys.dllhandle",
        "os.name",
        "os.startfile",
        "os.uname",
        "os.fork",
        "os.setsid",
        "os.killpg",
        "os.getuid",
        "os.geteuid",
        "os.getgid",
        "os.getegid",
        "os.setuid",
        "os.setgid",
        # The process groups, sessions, and child waits only POSIX defines; os.waitpid is portable.
        "os.getpgid",
        "os.setpgid",
        "os.getpgrp",
        "os.setpgrp",
        "os.getsid",
        "os.forkpty",
        "os.wait",
        "os.wait3",
        "os.wait4",
        "os.waitid",
        "os.WNOHANG",
        "os.WUNTRACED",
        "os.WCONTINUED",
        # The DLL search path only Windows has.
        "os.add_dll_directory",
        # The open flags only Windows defines.
        "os.O_BINARY",
        "os.O_TEXT",
        "os.O_NOINHERIT",
        # The open flags only POSIX defines.
        "os.O_CLOEXEC",
        "os.O_NOFOLLOW",
        "os.O_NONBLOCK",
        # The signals one platform defines and the other lacks; SIGINT, SIGTERM, SIGABRT, SIGFPE, SIGILL, and SIGSEGV
        # are defined on both.
        "signal.SIGKILL",
        "signal.SIGHUP",
        "signal.SIGQUIT",
        "signal.SIGALRM",
        "signal.SIGUSR1",
        "signal.SIGUSR2",
        "signal.SIGCHLD",
        "signal.SIGPIPE",
        "signal.SIGCONT",
        "signal.SIGSTOP",
        "signal.SIGTSTP",
        "signal.SIGTTIN",
        "signal.SIGTTOU",
        "signal.SIGWINCH",
        "signal.SIGBREAK",
        "signal.CTRL_C_EVENT",
        "signal.CTRL_BREAK_EVENT",
        # The signal functions only POSIX defines.
        "signal.alarm",
        "signal.setitimer",
        "signal.getitimer",
        "signal.pause",
        "signal.siginterrupt",
        "signal.sigwait",
        "signal.pthread_kill",
        "signal.pthread_sigmask",
        # What the platform module reports about the operating system and the machine; its python_* functions
        # describe the interpreter and are portable.
        "platform.system",
        "platform.machine",
        "platform.processor",
        "platform.release",
        "platform.version",
        "platform.uname",
        "platform.platform",
        "platform.win32_ver",
        "platform.win32_edition",
        "platform.win32_is_iot",
        "platform.mac_ver",
        "platform.libc_ver",
        "platform.freedesktop_os_release",
        "platform.android_ver",
        "platform.ios_ver",
        # The subprocess names only Windows defines, beside the families "qualified_prefix" holds.
        "subprocess.STARTUPINFO",
        "subprocess.DETACHED_PROCESS",
        "subprocess.SW_HIDE",
        "subprocess.ABOVE_NORMAL_PRIORITY_CLASS",
        "subprocess.BELOW_NORMAL_PRIORITY_CLASS",
        "subprocess.HIGH_PRIORITY_CLASS",
        "subprocess.IDLE_PRIORITY_CLASS",
        "subprocess.NORMAL_PRIORITY_CLASS",
        "subprocess.REALTIME_PRIORITY_CLASS",
        # The concrete paths each instantiate only on their own platform, and PureWindowsPath applies Windows path
        # rules on every platform, as ntpath does. PurePosixPath stays portable: it is how the repository writes its
        # own forward-slash relative paths, such as a manifest's keys.
        "pathlib.WindowsPath",
        "pathlib.PosixPath",
        "pathlib.PureWindowsPath",
    ),
    # Families of qualified names, each matched by its prefix: subprocess's Windows process creation flags, startup
    # flags, and standard handles.
    "qualified_prefix": ("subprocess.CREATE_", "subprocess.STARTF_", "subprocess.STD_"),
    "attribute": ("chmod", "fchmod", "lchmod", "st_file_attributes"),
    # subprocess keywords that only one platform accepts, matched as a keyword at the call and as a string literal,
    # which is how one is passed through a dict, as in subprocess.run(command, **{"creationflags": flags}).
    "keyword": ("creationflags", "startupinfo", "start_new_session", "process_group", "preexec_fn"),
    "module": (
        "ctypes",
        "winreg",
        "msvcrt",
        "_winapi",
        "_overlapped",
        "winsound",
        "nt",
        "posix",
        "fcntl",
        "pwd",
        "grp",
        "termios",
        "tty",
        "pty",
        "resource",
        "syslog",
        # Windows path rules on every platform; os.path is the portable module.
        "ntpath",
    ),
    # Windows environment names, which Windows compares ignoring case, so each is matched in any case.
    "variable": (
        "LOCALAPPDATA",
        "USERPROFILE",
        "ProgramFiles",
        "APPDATA",
        "GIT_BASH",
        "PATHEXT",
        "SystemRoot",
        "ComSpec",
        "WINDIR",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "ProgramData",
        "CommonProgramFiles",
        "SystemDrive",
        "HOMEDRIVE",
        "HOMEPATH",
    ),
    "command": ("cygpath", "taskkill", "tasklist"),
    # The filesystem paths only one platform has, matched where a path starts in a literal.
    "path": ("/proc",),
    # The extensions Windows finds a program by, matched in any case. Code that strips, appends, or compares one on a
    # program name assumes Windows. .ps1 is not one of them: it names a PowerShell script on every platform pwsh runs
    # on, so comparing a file's suffix with it tells a script's type, not the platform.
    "extension": (".exe", ".cmd", ".bat"),
}
# The string methods and the path method through which code strips, finds, or sets an extension.
EXTENSION_METHODS = frozenset({"endswith", "removesuffix", "rstrip", "replace", "with_suffix"})
# An extension at the start of the text joined after a program name, as in name + ".exe" or f"{name}.exe".
APPENDED_EXTENSION = re.compile(
    r"(" + "|".join(map(re.escape, PLATFORM_TOKENS["extension"])) + r")(?![A-Za-z0-9_])", re.IGNORECASE
)
# A platform path where one starts in a literal: at its start, or after whitespace, a quote, an equals sign, an
# opening parenthesis, a comma, or a shell separator, as in "/proc", f"/proc/{pid}/stat", or "cat /proc/1/stat".
PLATFORM_PATH = re.compile(
    r"(?:^|(?<=[\s\"'=(,;|&]))(" + "|".join(map(re.escape, PLATFORM_TOKENS["path"])) + r")(?![A-Za-z0-9_.-])"
)
# The calls that name an attribute or a module by a string literal: getattr(subprocess, "CREATE_NO_WINDOW", 0) and
# hasattr(os, "fork") name the attribute's token, and importlib.import_module("ntpath") the module's.
ATTRIBUTE_BY_NAME = frozenset({"getattr", "hasattr", "builtins.getattr", "builtins.hasattr"})
# What the platform policy cannot read: a token built at run time, such as an attribute, keyword, environment name,
# module, or path joined from parts, as in Path("/") / "proc", or held in a variable; a keyword dict another module
# builds, which is the intended route when that module is deployer/platform_support.py; and a module reached through a
# name the module does not bind by import.
# A module sanctions a use beside the code it excuses, as a module-level PLATFORM_ALLOWED = {token: reason}. An
# allowance the module no longer needs is reported.
PLATFORM_ALLOWANCE = "PLATFORM_ALLOWED"
# The module that defines PLATFORM_TOKENS, which names every token on purpose.
PLATFORM_POLICY_MODULE = "tests/validation/fsops_platform.py"


def _platform_scanned_files(root: Path) -> list[Path]:
    """The code that runs on a user's machine or runs validation, other than platform_support itself."""
    files = [root / "deploy.py", root / "tests" / "run_validation.py", root / "conftest.py"]
    files += [*(root / "tests" / "validation").glob("*.py")]
    files += [*(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    return [
        path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/platform_support.py"
    ]


def _platform_names(tree: ast.Module, skipped_tables: set[str]) -> list[tuple[int, str]]:
    """The platform tokens a module's code names, as (line, token), ignoring comments, docstrings, and test cases."""
    docstrings = docstring_ids(tree)
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


# The base a test case derives from directly: unittest's, named alone or as an attribute of its module.
TEST_CASE_NAMES = frozenset({"TestCase"})


def _is_test_case(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and any(
        (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) in TEST_CASE_NAMES
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
    if isinstance(node, ast.Call):
        named = _platform_names_by_string(node, aliases)
        return named + [(node.lineno, extension) for extension in _assumed_extensions(node)]
    if isinstance(node, (ast.Compare, ast.BinOp, ast.JoinedStr)):
        return [(node.lineno, extension) for extension in _assumed_extensions(node)]
    return []


def _platform_names_by_string(node: ast.Call, aliases: dict[str, str]) -> list[tuple[int, str]]:
    """The token a call of ATTRIBUTE_BY_NAME or MODULE_LOADERS names by its literal argument."""
    function = qualified_name(node.func, aliases)
    if function in MODULE_LOADERS:
        module = call_argument(node, 0, "name")
        if isinstance(module, ast.Constant) and isinstance(module.value, str):
            return _platform_module_name(node.lineno, module.value)
    if function in ATTRIBUTE_BY_NAME and len(node.args) > 1:
        owner, attribute = qualified_name(node.args[0], aliases), node.args[1]
        if owner and isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            return _platform_qualified_names(node.lineno, f"{owner}.{attribute.value}", attribute.value)
    return []


def _extension_literals(node: ast.expr) -> list[str]:
    """The executable extensions that a string literal, or each literal of a tuple, list, or set or of a call's one
    argument, such as frozenset({...}), ends with, in any case."""
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        return _extension_literals(node.args[0])
    parts = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else [node]
    return [
        extension
        for part in parts
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
        for extension in PLATFORM_TOKENS["extension"]
        if part.value.casefold().endswith(extension)
    ]


def _appended_extension(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        match = APPENDED_EXTENSION.match(node.value)
        return [match.group(1).casefold()] if match else []
    return []


def _assumed_extensions(node: ast.Call | ast.Compare | ast.BinOp | ast.JoinedStr) -> list[str]:
    """The executable extensions node strips, finds, sets, compares, or appends: a literal argument of one of
    EXTENSION_METHODS, a literal operand of a comparison, or one joined after a value by + or in an f-string. A string
    that only holds an extension, such as a pattern or a path, assumes nothing."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in EXTENSION_METHODS:
            return [extension for argument in node.args for extension in _extension_literals(argument)]
        return []
    if isinstance(node, ast.Compare):
        return [extension for operand in [node.left, *node.comparators] for extension in _extension_literals(operand)]
    if isinstance(node, ast.BinOp):
        return _appended_extension(node.right) if isinstance(node.op, ast.Add) else []
    return [
        extension
        for previous, part in pairwise(node.values)
        if isinstance(previous, ast.FormattedValue)
        for extension in _appended_extension(part)
    ]


def _platform_module_name(line: int, module: str) -> list[tuple[int, str]]:
    top = module.split(".")[0]
    return [(line, top)] if top in PLATFORM_TOKENS["module"] else []


def _platform_attribute_names(node: ast.Attribute, aliases: dict[str, str]) -> list[tuple[int, str]]:
    return _platform_qualified_names(node.lineno, qualified_name(node, aliases), node.attr)


def _platform_qualified_names(line: int, qualified: str, attribute: str) -> list[tuple[int, str]]:
    """The token an attribute names: its qualified name, one of a qualified family, or a platform attribute."""
    if qualified in PLATFORM_TOKENS["qualified"] or qualified.startswith(PLATFORM_TOKENS["qualified_prefix"]):
        return [(line, qualified)]
    if attribute in PLATFORM_TOKENS["attribute"]:
        return [(line, attribute)]
    return []


def _platform_import_from_names(node: ast.ImportFrom) -> list[tuple[int, str]]:
    """The module's token, or else each imported name that is a token. A relative import names a module of this
    repository, never a platform's."""
    if node.level:
        return []
    module = node.module or ""
    found = _platform_module_name(node.lineno, module)
    if found:
        return found
    for alias in node.names:
        found.extend(_platform_qualified_names(node.lineno, f"{module}.{alias.name}", alias.name))
    return found


def _platform_string_names(line: int, value: str, command: re.Pattern[str]) -> list[tuple[int, str]]:
    # Equality catches an environment lookup or entry, never prose that mentions the variable, and it ignores case
    # because Windows does: os.environ.get("PROGRAMFILES") reads ProgramFiles.
    variables = {variable.casefold() for variable in PLATFORM_TOKENS["variable"]}
    found = [(line, value)] if value.casefold() in variables else []
    found.extend((line, value) for keyword in PLATFORM_TOKENS["keyword"] if value == keyword)
    found.extend((line, name) for name in sorted(set(command.findall(value))))
    found.extend((line, path) for path in sorted(set(PLATFORM_PATH.findall(value))))
    return found


def platform_code_problems(root: Path) -> list[str]:
    """Report operating-system-specific code outside deployer/platform_support.py, and allowances no longer needed.

    CLAUDE.md keeps every operating-system-specific behavior in that one module, so a port changes only it.
    """
    found: set[tuple[str, int, str]] = set()
    problems: list[str] = []
    ordered = sorted(_platform_scanned_files(root), key=lambda path: path.relative_to(root).as_posix())
    for _, name, tree in python_trees(ordered, root, problems):
        allowed = module_allowance(tree, PLATFORM_ALLOWANCE)
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
# The logging handlers that create or append to the file they are given.
LOGGING_FILE_HANDLERS = (
    "logging.FileHandler",
    "logging.handlers.RotatingFileHandler",
    "logging.handlers.TimedRotatingFileHandler",
    "logging.handlers.WatchedFileHandler",
)
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
            "lchmod",
            "copy_into",
            "move_into",
            # An archive's members, extracted from a tarfile.TarFile or a zipfile.ZipFile.
            "extractall",
            "extract",
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
            "os.renames",
            "os.mkfifo",
            "os.mknod",
            "os.lchmod",
            "os.chflags",
            "os.lchflags",
            "os.setxattr",
            "os.removexattr",
            "shutil.rmtree",
            "shutil.move",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.copytree",
            "shutil.chown",
            "shutil.unpack_archive",
            "shutil.make_archive",
            "sqlite3.connect",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
            "tempfile.TemporaryDirectory",
            "tempfile.NamedTemporaryFile",
            "tempfile.TemporaryFile",
            "tempfile.SpooledTemporaryFile",
            *LOGGING_FILE_HANDLERS,
            "urllib.request.urlretrieve",
            # A compiler writes the bytecode beside its source or where it is told.
            "py_compile.compile",
            "compileall.compile_dir",
            "compileall.compile_file",
            "compileall.compile_path",
            # The logging configurations, which create the files their handlers name, from a configuration the policy
            # cannot follow.
            "logging.config.dictConfig",
            "logging.config.fileConfig",
            # An application archive, and a virtual environment, which EnvBuilder exists to create.
            "zipapp.create_archive",
            "venv.create",
            "venv.EnvBuilder",
            # ejryan89-skill-core's atomic replace, which the skills' scripts share: the deployer's writes are
            # journaled, so none of them may bypass fsops through it.
            "atomic_file.replace_bytes",
            "atomic_file.replace_text",
        }
    ),
}
# A module sanctions a write beside the code it excuses, as a module-level FSOPS_ALLOWED = {token: reason}.
FSOPS_ALLOWANCE = "FSOPS_ALLOWED"
WRITE_MODE_CHARACTERS = "wax+"
# Path.copy and Path.move, whose names a dict's or a list's copy() and a module's own move function share, so
# _path_copies tells them apart by their target argument and their receiver instead.
PATH_COPIES = frozenset({"copy", "move"})
# The functions that open a file by name with its mode second; a Path's open() takes the mode first.
OPEN_FUNCTIONS = frozenset({"open", "builtins.open", "io.open", "codecs.open"})


def _mode_writes(mode: str) -> bool:
    return any(character in mode for character in WRITE_MODE_CHARACTERS)


def _tar_mode_writes(mode: str) -> bool:
    """A tarfile mode writes by what comes before its compression, so "r:xz" reads and "x:gz" writes."""
    return _mode_writes(re.split(r"[:|]", mode, maxsplit=1)[0])


def _flag_writes(flag: str) -> bool:
    """A dbm or shelve flag other than "r" may create or write the database."""
    return not flag.startswith("r")


@dataclass(frozen=True)
class ModeOpener:
    """A function that opens an archive, a compressed file, or a database by name with a mode that may write: the
    position and keyword of its mode, the keywords that name its file, whether it writes when given no mode, and which
    modes write."""

    position: int
    keyword: str
    files: frozenset[str]
    writes_by_default: bool
    writes: Callable[[str], bool]


_ARCHIVE = ModeOpener(1, "mode", frozenset({"file"}), False, _mode_writes)
_TAR = ModeOpener(1, "mode", frozenset({"name", "fileobj"}), False, _tar_mode_writes)
_COMPRESSED = ModeOpener(1, "mode", frozenset({"filename", "fileobj"}), False, _mode_writes)
MODE_OPENERS: dict[str, ModeOpener] = {
    "zipfile.ZipFile": _ARCHIVE,
    "zipfile.PyZipFile": _ARCHIVE,
    "tarfile.open": _TAR,
    "tarfile.TarFile": _TAR,
    "tarfile.TarFile.open": _TAR,
    "gzip.open": _COMPRESSED,
    "gzip.GzipFile": _COMPRESSED,
    "bz2.open": _COMPRESSED,
    "bz2.BZ2File": _COMPRESSED,
    "lzma.open": _COMPRESSED,
    "lzma.LZMAFile": _COMPRESSED,
    "shelve.open": ModeOpener(1, "flag", frozenset({"filename"}), True, _flag_writes),
    "dbm.open": ModeOpener(1, "flag", frozenset({"file"}), False, _flag_writes),
    "io.FileIO": ModeOpener(1, "mode", frozenset({"file"}), False, _mode_writes),
}
# logging.basicConfig writes only when given a file, which it takes by keyword alone.
BASIC_CONFIG = "logging.basicConfig"
# The fileinput readers write only in place, which their second argument or inplace keyword asks for.
IN_PLACE_READERS = frozenset({"fileinput.input", "fileinput.FileInput"})
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


def _mode_argument(call: ast.Call, position: int, name: str = "mode") -> ast.expr | None:
    """The argument that gives a call's mode, or None when it gives none. An unpacking that may hold the mode is
    returned as the mode, which no mode reader takes for a literal."""
    return call_argument(call, position, name)


def _writes_with_mode(
    mode: ast.expr | None, bindings: dict[str, set[str] | None], writes: Callable[[str], bool] = _mode_writes
) -> bool:
    """Whether a mode may write. No mode reads, and a mode the policy cannot read, such as a parameter, writes."""
    if mode is None:
        return False
    values: set[str] | None = None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        values = {mode.value}
    elif isinstance(mode, ast.Name):
        values = bindings.get(mode.id)
    return values is None or any(writes(value) for value in values)


def _os_open_writes(call: ast.Call, resolver: CallableResolver) -> bool:
    """Whether os.open is given flags that may write: any flag outside READ_FLAGS, a nonzero number, or flags held in
    anything but os.O_* names joined by |."""
    flags = call_argument(call, 1, "flags")
    if flags is None:
        return True
    pending: list[ast.expr] = [flags]
    while pending:
        part = pending.pop()
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
            pending += [part.left, part.right]
        elif isinstance(part, ast.Constant) and part.value == 0 and type(part.value) is int:
            continue
        elif resolver.name(part) not in READ_FLAGS:
            return True
    return False


def _replaces_a_path(call: ast.Call) -> bool:
    """Whether a .replace() call may be Path.replace(target): one argument, where str.replace takes two, or its target
    as a keyword, or a `**` unpacking that may hold it beside fewer than two positional arguments."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "replace"):
        return False
    if any(keyword.arg == "target" for keyword in call.keywords):
        return True
    positional = [argument for argument in call.args if not isinstance(argument, ast.Starred)]
    if any(keyword.arg is None for keyword in call.keywords):
        return len(positional) < 2
    return len(call.args) == 1 and len(positional) == 1 and not call.keywords


def _path_copies(call: ast.Call, resolver: CallableResolver) -> bool:
    """Whether a .copy() or .move() call may be Path.copy(target) or Path.move(target): it gives a target, which a
    dict's or a list's copy() never takes, and it is not a function of a module the module imports, such as
    `from . import fsops` then fsops.move(...), or copy.copy(value). A class named in CapWords, as pathlib.Path is,
    is not a module."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr in PATH_COPIES):
        return False
    if not (call.args or call.keywords):
        return False
    root = call.func.value
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name):
        owner = resolver.scopes[id(root)].owner(root.id)
        module = resolver.name(call.func.value).removeprefix("<local>.")
        if owner is not None and root.id in owner.imports and module and module.islower():
            return False
    return True


def _rewrites_in_place(call: ast.Call) -> bool:
    """Whether a fileinput reader is given an inplace argument that is not a false constant."""
    inplace = _mode_argument(call, 1, "inplace")
    return inplace is not None and not (isinstance(inplace, ast.Constant) and not inplace.value)


def _call_writes(call: ast.Call, resolver: CallableResolver, bindings: dict[str, set[str] | None]) -> str | None:
    """The token naming the write a call makes, or None when it makes none."""
    target = resolver.name(call.func)
    if target in FILESYSTEM_WRITES["qualified"]:
        return target
    if target in METHOD_NAMERS:
        return _named_method_writes(call, target)
    if target == BASIC_CONFIG:
        return target if any(keyword.arg in {"filename", None} for keyword in call.keywords) else None
    if target in IN_PLACE_READERS:
        return target if _rewrites_in_place(call) else None
    if target == "os.open":
        return target if _os_open_writes(call, resolver) else None
    if target in OPEN_FUNCTIONS:
        return "open" if _writes_with_mode(_mode_argument(call, 1), bindings) else None
    if opener := MODE_OPENERS.get(target):
        mode = _mode_argument(call, opener.position, opener.keyword)
        writes = opener.writes_by_default if mode is None else _writes_with_mode(mode, bindings, opener.writes)
        return target if writes else None
    if isinstance(call.func, ast.Attribute):
        if call.func.attr in FILESYSTEM_WRITES["method"] or _replaces_a_path(call) or _path_copies(call, resolver):
            return call.func.attr
        if call.func.attr == "open" and _writes_with_mode(_mode_argument(call, 0), bindings):
            return "open"
    return None


# The method names a write callable passed as a value may have besides the write methods: a Path's open() and
# replace(), which a call tells from a read or a str.replace by its arguments, and a value reference has none.
REFERENCED_METHODS = FILESYSTEM_WRITES["method"] | {"open", "replace"}
# Path.copy and Path.move passed as values, which a reference names only through the class.
REFERENCED_PATH_COPIES = frozenset({"pathlib.Path.copy", "pathlib.Path.move"})
# The callables that make a callable of a method or an attribute named by a string: methodcaller("write_text", text)
# calls path.write_text(text) on the path it is given, and attrgetter("unlink") returns path.unlink.
METHOD_NAMERS = frozenset({"operator.methodcaller", "operator.attrgetter"})
WRITE_CALLABLES = FILESYSTEM_WRITES["qualified"] | MODE_OPENERS.keys() | IN_PLACE_READERS | {"os.open", BASIC_CONFIG}
# The modules that hold a write callable, each of them with every package above it: a module passed as a value, as in
# self.fs = shutil, is called through an attribute the policy cannot follow. A class, named in CapWords, as
# tarfile.TarFile is, is not a module; pathlib is left out, since a Path writes only through the methods the policy
# reads on any receiver.
WRITING_MODULES = frozenset(
    prefix
    for name in WRITE_CALLABLES | OPEN_FUNCTIONS
    for prefix in (".".join(name.split(".")[:end]) for end in range(1, name.count(".") + 1))
    if prefix.islower()
)


def _named_method_writes(call: ast.Call, maker: str) -> str | None:
    """The token naming the write a methodcaller or an attrgetter makes: the write method a literal names, or the
    maker itself when a name is not a literal. methodcaller names one method, and a copy() or move() only with the
    target a Path's takes; attrgetter names one attribute or more, any of them dotted."""
    names = call.args[:1] if maker == "operator.methodcaller" else call.args
    methods = REFERENCED_METHODS | (PATH_COPIES if len(call.args) > 1 and maker == "operator.methodcaller" else set())
    for name in names:
        literal = _literal_name(name)
        if literal is None:
            return maker
        if literal.rsplit(".", 1)[-1] in methods:
            return literal.rsplit(".", 1)[-1]
    return None


def _reference_writes(node: ast.expr, resolver: CallableResolver) -> str | None:
    """The token naming the write a callable may make when it is passed as a value rather than called, such as
    map(Path.write_text, ...) or a callback=shutil.rmtree, or None when it names no write. Whoever calls it chooses
    its arguments, so a callable that writes with some mode or flag counts, and so does a module that holds one."""
    target = resolver.name(node)
    if target in OPEN_FUNCTIONS:
        return "open"
    if target in WRITE_CALLABLES | METHOD_NAMERS | WRITING_MODULES:
        return target
    if target in REFERENCED_PATH_COPIES:
        return target.rsplit(".", 1)[-1]
    if isinstance(node, ast.Attribute) and node.attr in REFERENCED_METHODS:
        return node.attr
    return None


def _literal_name(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_attribute_owner(
    call: ast.Call, resolver: CallableResolver, function: str = ""
) -> tuple[ast.expr, str] | None:
    """The owner a getattr or hasattr call reads an attribute of by a literal name, with that name, as in
    getattr(shutil, "rmtree"), or None when the call is neither or names the attribute some other way. A caller that
    has read the callable some other way, as through a literal lookup, gives its name as function."""
    if (function or resolver.name(call.func)) not in ATTRIBUTE_BY_NAME or any(
        isinstance(a, ast.Starred) for a in call.args
    ):
        return None
    attribute = _literal_name(call.args[1]) if len(call.args) > 1 else None
    return (call.args[0], attribute) if attribute is not None else None


def _literal_callee(node: ast.expr, resolver: CallableResolver) -> ast.expr | None:
    """A callable read as the dotted name it names by a literal: getattr(owner, "name") and owner.__dict__["name"] as
    owner.name, and importlib.import_module("name"), __import__("name"), and sys.modules["name"] as the module, each
    also as the receiver of an attribute, as in importlib.import_module("shutil").rmtree, and each also when a literal
    lookup gives the loader, as in getattr(importlib, "import_module")("shutil"); None for any other expression. Each
    node it builds takes the scope of the expression it stands for, so the resolver reads it as it reads that
    expression."""
    scope = resolver.scopes[id(node)]

    def built(new: ast.expr) -> ast.expr:
        resolver.scopes[id(new)] = scope
        return ast.copy_location(new, node)

    def attribute_of(owner: ast.expr, attribute: str) -> ast.expr:
        receiver = _literal_callee(owner, resolver) or owner
        return built(ast.Attribute(value=receiver, attr=attribute, ctx=ast.Load()))

    def module_named(module: str) -> ast.expr:
        parts = module.split(".")
        name: ast.expr = built(ast.Name(id=parts[0], ctx=ast.Load()))
        for part in parts[1:]:
            name = built(ast.Attribute(value=name, attr=part, ctx=ast.Load()))
        return name

    if isinstance(node, ast.Attribute):
        receiver = _literal_callee(node.value, resolver)
        return built(ast.Attribute(value=receiver, attr=node.attr, ctx=ast.Load())) if receiver is not None else None
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
            key = _literal_name(node.slice)
            return attribute_of(node.value.value, key) if key is not None else None
        looked_up = module_lookup(node, resolver)
        return module_named(looked_up) if looked_up and looked_up != COMPUTED_NAME else None
    if not isinstance(node, ast.Call):
        return None
    loader = resolver.name(node.func)
    if not loader and (named := _literal_callee(node.func, resolver)) is not None:
        loader = resolver.name(named)
    if loader in ATTRIBUTE_LOOKUPS and (read := _literal_attribute_owner(node, resolver, loader)) is not None:
        return attribute_of(*read)
    module = _literal_name(call_argument(node, 0, "name")) if loader in MODULE_LOADERS else None
    if not module:
        return None
    # __import__ returns the top package of a dotted name; import_module returns the module itself.
    return module_named(module if loader.endswith("import_module") else module.partition(".")[0])


def _unread_lookup(node: ast.expr, resolver: CallableResolver) -> str | None:
    """What a callable reaches by a lookup _literal_callee cannot read, described for a report: a subscript, as in
    handlers[kind] or shutil.__dict__[name], or a module named by anything but one string literal, as in
    importlib.import_module(name) or sys.modules[name], each also as the receiver of an attribute; None for any other
    expression, which the resolver reads or reports itself."""
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if _literal_callee(node, resolver) is not None:
        return None
    if module_lookup(root, resolver) == COMPUTED_NAME:
        return f"the module {ast.unparse(root)} names"
    return f"the callable {ast.unparse(node)} looks up" if isinstance(node, ast.Subscript) else None


def _callable_references(tree: ast.Module, resolver: CallableResolver) -> set[int]:
    """The ids of the names, attributes, subscripts, and calls a module reads as values: neither a call's callable,
    which _call_writes reads, nor an attribute's receiver, nor a name or an attribute an alias is bound to, which the
    resolver follows to the calls it makes, nor the owner getattr or hasattr reads by a literal name. A call or a
    subscript bound to a name is read here, as in rm = getattr(shutil, "rmtree") or fs = sys.modules["shutil"], since
    the resolver does not follow it."""
    excluded = {
        id(value)
        for scope in resolver.scopes.values()
        for values in scope.bindings.values()
        for value in values
        if isinstance(value, (ast.Name, ast.Attribute))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            excluded.add(id(node.func))
            if (read := _literal_attribute_owner(node, resolver)) is not None:
                excluded.add(id(read[0]))
        elif isinstance(node, ast.Attribute):
            excluded.add(id(node.value))
    return {
        id(node)
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            or (isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Load))
        )
        and id(node) not in excluded
    }


def _write_nodes(tree: ast.Module, resolver: CallableResolver | None = None) -> list[tuple[ast.stmt | ast.expr, str]]:
    """The imports, calls, and callables passed as values through which a module's code writes to the filesystem,
    with each one's token, ignoring test cases. A callable the resolver cannot name is read through _literal_callee."""
    resolver = resolver or CallableResolver(tree)
    bindings = _string_bindings(tree)
    references = _callable_references(tree, resolver)
    found: list[tuple[ast.stmt | ast.expr, str]] = []

    def read(node: ast.expr) -> ast.expr:
        return node if resolver.name(node) else (_literal_callee(node, resolver) or node)

    def visit(node: ast.AST) -> None:
        if _is_test_case(node):
            return
        if isinstance(node, ast.ImportFrom):
            found.extend(
                (node, f"{node.module}.{alias.name}")
                for alias in node.names
                if f"{node.module}.{alias.name}" in FILESYSTEM_WRITES["qualified"]
            )
        elif isinstance(node, ast.Call) and not resolver.unresolved(node):
            function = read(resolver.function(node))
            call = ast.copy_location(ast.Call(func=function, args=node.args, keywords=node.keywords), node)
            if token := _call_writes(call, resolver, bindings):
                found.append((call, token))
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)) and id(node) in references:
            value = read(node)
            if token := _reference_writes(value, resolver):
                found.append((value, token))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _returned_callee_problems(name: str, tree: ast.Module, resolver: CallableResolver, skipped: set[int]) -> list[str]:
    """Report each call of a callable another call returns that _literal_callee cannot read, as in factory()(path)
    or operator.methodcaller("unlink")(path), and each call of a callable reached by a lookup it cannot read, as
    _unread_lookup describes: the policy cannot tell what it calls, so it is never guessed."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in skipped:
            continue
        if isinstance(node.func, ast.Call):
            if _literal_callee(node.func, resolver) is None:
                problems.append(
                    f"{name}:{node.lineno} cannot resolve the callable {ast.unparse(node.func)} returns; "
                    "call a known callable instead"
                )
        elif (lookup := _unread_lookup(node.func, resolver)) is not None:
            problems.append(f"{name}:{node.lineno} cannot resolve {lookup}; call a known callable instead")
    return problems


def _filesystem_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """The filesystem writes a module's code makes, as (line, token), ignoring test cases."""
    return [(node.lineno, token) for node, token in _write_nodes(tree)]


TEMPORARY_DIRECTORY_CALL = "tempfile.mkdtemp"
# The calls whose one argument and result name the same path, resolved through the module's imports.
PATH_KEEPING_CALLS = frozenset({"pathlib.Path", "os.fspath", "str"})


# The tempfile keywords joined to the name of what it makes, which a path separator would carry out of the system
# temporary directory, as in prefix="../escape-".
NAME_PARTS = frozenset({"prefix", "suffix"})
PATH_SEPARATORS = ("/", "\\")


def _separator_free(value: ast.expr) -> bool:
    """Whether a prefix or suffix is None or a literal that holds no path separator."""
    if not isinstance(value, ast.Constant):
        return False
    if isinstance(value.value, str):
        return not any(separator in value.value for separator in PATH_SEPARATORS)
    if isinstance(value.value, bytes):
        return not any(separator.encode() in value.value for separator in PATH_SEPARATORS)
    return value.value is None


def _in_the_temporary_directory(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a tempfile call takes keyword arguments only, none of them dir, and a prefix or suffix only as a literal
    free of path separators, so it writes under the system temporary directory. A positional argument may be dir, so
    none is accepted."""
    return qualified_name(call.func, aliases).startswith("tempfile.") and (
        not call.args
        and all(keyword.arg not in {"dir", None} for keyword in call.keywords)
        and all(_separator_free(keyword.value) for keyword in call.keywords if keyword.arg in NAME_PARTS)
    )


def _bound_names(node: ast.AST) -> list[str]:
    """The names node binds: a name stored to by any assignment, loop, with, or assignment expression, a parameter, a
    caught exception, an import, a function or class, or a pattern's capture."""
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _temporary_directory_names(tree: ast.Module, resolver: CallableResolver) -> set[str]:
    """The names a module binds only to a directory tempfile.mkdtemp created under the system temporary directory,
    directly or through calls that keep the path, such as Path(...), and never by any other binding _bound_names
    reads, such as a parameter, a loop or with target, a caught exception, an import, a definition, or a pattern's
    capture. Any other call around it, such as a function of the module's own, may return another path."""
    assigned = {
        id(target): node.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    values: dict[str, list[ast.expr | None]] = {}
    for node in ast.walk(tree):
        for name in _bound_names(node):
            values.setdefault(name, []).append(assigned.get(id(node)))

    def created(value: ast.expr | None) -> bool:
        if not isinstance(value, ast.Call):
            return False
        if resolver.name(value.func) == TEMPORARY_DIRECTORY_CALL:
            return _in_the_temporary_directory(value, resolver.aliases(value.func))
        return (
            resolver.name(value.func) in PATH_KEEPING_CALLS
            and len(value.args) == 1
            and not value.keywords
            and created(value.args[0])
        )

    return {name for name, bound in values.items() if all(created(value) for value in bound)}


# The path methods that take a second path, where the call writes besides its receiver.
TWO_PATH_METHODS = frozenset({"rename", "replace", "symlink_to", "hardlink_to"})


# The writes that name where they land in one argument, by its position, or None when only a keyword gives it, and
# the keywords that may give it: open() and os.open() write to their file, and os.open() also relative to its dir_fd;
# an archive, compressed file, or database opener to its file; unpack_archive to its extract_dir; make_archive to its
# base_name; a logging handler and basicConfig to their filename; fileinput to its files; and urlretrieve to its
# filename.
NAMED_DESTINATIONS: dict[str, tuple[int | None, frozenset[str]]] = {
    "os.open": (0, frozenset({"path", "dir_fd"})),
    **{name: (0, frozenset({"file"})) for name in OPEN_FUNCTIONS},
    **{name: (0, opener.files) for name, opener in MODE_OPENERS.items()},
    "shutil.unpack_archive": (1, frozenset({"extract_dir"})),
    "shutil.make_archive": (0, frozenset({"base_name"})),
    **dict.fromkeys(LOGGING_FILE_HANDLERS, (0, frozenset({"filename"}))),
    BASIC_CONFIG: (None, frozenset({"filename"})),
    **dict.fromkeys(IN_PLACE_READERS, (0, frozenset({"files"}))),
    "urllib.request.urlretrieve": (1, frozenset({"filename"})),
}
# The archive methods that write to their path argument, never to their receiver, which is the archive they read.
METHOD_DESTINATIONS: dict[str, tuple[int, frozenset[str]]] = {
    "extractall": (0, frozenset({"path"})),
    "extract": (1, frozenset({"path"})),
}


def _write_destinations(call: ast.Call, aliases: dict[str, str]) -> list[ast.expr]:
    """The arguments that may name where a write lands. A write in NAMED_DESTINATIONS or METHOD_DESTINATIONS lands at
    the argument it names; a path method writes to its receiver and, for a rename or a link, to its target; any other
    function may write to every argument but a constant that is not a string, such as a mode or a flag."""
    target = qualified_name(call.func, aliases)
    if target in NAMED_DESTINATIONS:
        return call_arguments(call, *NAMED_DESTINATIONS[target])
    if isinstance(call.func, ast.Attribute) and call.func.attr in METHOD_DESTINATIONS:
        return call_arguments(call, *METHOD_DESTINATIONS[call.func.attr])
    if target not in FILESYSTEM_WRITES["qualified"] and isinstance(call.func, ast.Attribute):
        second = [*call.args, *call_arguments(call, None, {"target"})] if call.func.attr in TWO_PATH_METHODS else []
        return [call.func.value, *second]
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
    else may be anywhere, and so may a sanctioned callable passed as a value, whose caller chooses its arguments."""
    resolver = CallableResolver(tree)
    directories = _temporary_directory_names(tree, resolver)
    problems: list[str] = []
    for node, token in _write_nodes(tree, resolver):
        if isinstance(node, ast.ImportFrom) or token not in allowed:
            continue
        if not isinstance(node, ast.Call):
            problems.append(
                f"{name}:{node.lineno} {FSOPS_ALLOWANCE} allows {token} only under the system temporary directory, "
                "and a callable passed as a value may write anywhere"
            )
            continue
        aliases = resolver.aliases(node.func)
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
    ordered = sorted(_writes_scanned_files(root), key=lambda path: path.relative_to(root).as_posix())
    for _, name, tree in python_trees(ordered, root, problems):
        allowed = module_allowance(tree, FSOPS_ALLOWANCE)
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
        skipped = {id(child) for node in ast.walk(tree) if _is_test_case(node) for child in ast.walk(node)}
        resolver = CallableResolver(tree)
        problems += resolver.problems(name, skipped) + _returned_callee_problems(name, tree, resolver, skipped)
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

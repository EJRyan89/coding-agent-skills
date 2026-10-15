"""What a skill's scripts may do: the commands they run, how they read gh output, how they run git and gh, the
"Script results" contract, the console setup, and the function names CodeQL reads as secrets.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import shlex
import tempfile
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from duplication import SKILL_CORE, SKILL_CORE_SCRIPTS
from toolchain import find_powershell
from validation_support import (
    REPOSITORY_ROOT,
    CallableResolver,
    find_fences,
    import_aliases,
    is_test_script,
    module_allowance,
    module_imports,
    qualified_name,
    skill_script_directories,
    skills_by_name,
)

from deployer import platform_support, render

SHELL_OPERATORS = ("&&", "||", ";;", "|&", "()", "(", ")", ";", "&", "|", "<", ">", "\n")
COMMAND_SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", "\n", "{"}
LEADING_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "time", "!", "}", ")", "fi", "done"}
# Keywords whose following words, up to the next separator, are not commands: "for name in list", "case word in".
HEADER_KEYWORDS = {"case", "for", "function", "select"}
SHELL_BUILTINS = {
    "alias",
    "break",
    "cd",
    "command",
    "continue",
    "declare",
    "dirs",
    "echo",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "getopts",
    "hash",
    "let",
    "local",
    "mapfile",
    "popd",
    "printf",
    "pushd",
    "pwd",
    "read",
    "readarray",
    "readonly",
    "return",
    "set",
    "shift",
    "shopt",
    "source",
    "test",
    "trap",
    "true",
    "type",
    "ulimit",
    "umask",
    "unalias",
    "unset",
    "wait",
}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
COMMAND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
COMMANDS_DOC = '"Commands skills may run" in docs/adding-a-skill.md'


def _split_operators(token: str) -> list[str]:
    """Split a run of shell punctuation, which shlex returns as one token, into its operators."""
    if not token or any(char not in ";&|()<>\n" for char in token):
        return [token]
    parts: list[str] = []
    while token:
        operator = next(op for op in SHELL_OPERATORS if token.startswith(op))
        parts.append(operator)
        token = token[len(operator) :]
    return parts


def shell_commands(script: str) -> set[str]:
    """Literal command names a Bash script runs, ignoring keywords, case patterns, and its own functions."""
    lexer = shlex.shlex(script.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    tokens = [part for token in lexer for part in _split_operators(token)]
    functions = {tokens[i] for i in range(len(tokens) - 1) if tokens[i + 1] == "()"}
    functions |= {tokens[i + 1] for i in range(len(tokens) - 1) if tokens[i] == "function"}
    found: set[str] = set()
    state = "command"
    for word in tokens:
        if state == "case-word":
            state = "patterns" if word == "in" else state
        elif state == "patterns":
            state = "command" if word == ")" else "arguments" if word == "esac" else state
        elif word == ";;":
            state = "patterns"
        elif word == "esac":
            state = "arguments"
        elif state == "command":
            if word in COMMAND_SEPARATORS or word in LEADING_KEYWORDS or ASSIGNMENT.match(word):
                continue
            state = "case-word" if word == "case" else "arguments"
            if word not in HEADER_KEYWORDS and COMMAND_NAME.fullmatch(word) and word not in functions:
                found.add(word)
        elif word in COMMAND_SEPARATORS:
            state = "command"
    return found


def _fences(markdown: str, context: str) -> list[str]:
    """The body of each closed fence that deployer/render.py renders in a context: shell for a Bash, sh, or shell
    fence, and powershell for a PowerShell, ps1, or pwsh fence."""
    lines = markdown.splitlines()
    return [
        "\n".join(fence.text(lines[index]) for index in fence.body())
        for fence in find_fences(lines)
        if fence.closer is not None and render.fence_context(fence) == context
    ]


# The calls that start a command, as the module's imports resolve them, and the position and keyword of the argument
# that names it: a list or tuple whose first item is the program, or a string whose first word is. The exec and spawn
# calls of os take the program itself, by path or by name, ahead of its arguments.
COMMAND_RUNNERS: dict[str, tuple[int, str]] = {
    **{f"subprocess.{name}": (0, "args") for name in ("run", "Popen", "call", "check_call", "check_output")},
    "subprocess.getoutput": (0, "cmd"),
    "subprocess.getstatusoutput": (0, "cmd"),
    "os.system": (0, "command"),
    "os.popen": (0, "cmd"),
    "os.execv": (0, "path"),
    "os.execve": (0, "path"),
    **{f"os.{name}": (0, "file") for name in ("execl", "execle", "execlp", "execlpe", "execvp", "execvpe")},
    **{
        f"os.{name}": (1, "file")
        for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")
    },
    "os.posix_spawn": (0, "path"),
    "os.posix_spawnp": (0, "path"),
    "asyncio.create_subprocess_exec": (0, "program"),
    "asyncio.create_subprocess_shell": (0, "cmd"),
    # skill-core's runner, which bounds a command with a time limit and gives it no stdin.
    "bounded_process.run_bounded": (0, "command"),
    "bounded_process.streaming": (0, "command"),
}
# The runners that hand their keywords to subprocess.Popen, whose executable= replaces the program the command names.
EXECUTABLE_RUNNERS = frozenset(
    {
        *(f"subprocess.{name}" for name in ("run", "Popen", "call", "check_call", "check_output")),
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
# skill-core's clients: each runs its one command for every call a script makes through it.
CLIENT_CLASSES = {"git_client.GitClient": "git", "github_client.GitHubClient": "gh"}
# shutil.which finds the program a script then runs. A script that takes it as an injected service calls it by its
# own name, such as services.which("dotnet"), so a call of any function named which counts too, and so do
# skill-core's locator's find_executable, which the deployer's platform_support also names, find_executables, which
# lists every match in its search order, and path_programs, which lists every match on PATH. A lookup takes the
# program's name first, or by one of LOOKUP_KEYWORDS.
COMMAND_LOOKUP = "shutil.which"
LOOKUP_NAMES = frozenset({"which", "path_programs", "find_executable", "find_executables"})
LOOKUP_KEYWORDS = frozenset({"cmd", "name"})
# Calls whose result names the program their first argument does: a path or text conversion, a copy into a list or a
# tuple, a split or a join of a command, and skill-core's located_command, which names a command's program by the
# path the locator finds for it; and the path methods that do the same for the path they are called on.
PROGRAM_CONVERSIONS = frozenset(
    {
        "executable_locator.located_command",
        "str",
        "list",
        "tuple",
        "os.fspath",
        "os.path.abspath",
        "os.path.normpath",
        "os.path.realpath",
        "pathlib.Path",
        "pathlib.PurePath",
        "shlex.join",
        "shlex.split",
    }
)
PATH_METHODS = frozenset({"absolute", "resolve"})
# The interpreter running the script, which "Commands skills may run" calls by the platform's Python command.
INTERPRETER = "sys.executable"
INTERPRETER_PROGRAM = platform_support.PYTHON
# A module states why it starts a command whose program the policy cannot read beside the code, as a module-level
# COMMANDS_ALLOWED = {scope: reason}, where a scope is the function or class holding what cannot be read, by qualified
# name, or a module-level assignment's target. An allowance the module no longer needs is reported.
COMMANDS_ALLOWANCE = "COMMANDS_ALLOWED"
MODULE_SCOPE = "<module>"


@dataclass(frozen=True)
class Unreadable:
    """A place the program a command starts cannot be read from, and what it holds."""

    node: ast.AST
    description: str

    @property
    def line(self) -> int:
        return int(getattr(self.node, "lineno", 0))


# What the reader finds for a command: the string literal that names its program, or where it cannot read one.
Source = ast.Constant | Unreadable
Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


@dataclass(frozen=True)
class _Element:
    """A name bound to each element of a collection in turn, by a for loop or a comprehension."""

    collection: ast.expr


Binding = ast.expr | _Element | None


def _is_lookup(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call looks a program up: shutil.which under any name its imports give it, or a function named in
    LOOKUP_NAMES."""
    function = call.func
    if isinstance(function, ast.Attribute) and function.attr in LOOKUP_NAMES:
        return True
    return (isinstance(function, ast.Name) and function.id in LOOKUP_NAMES) or (
        qualified_name(function, aliases) == COMMAND_LOOKUP
    )


def _lookup_argument(call: ast.Call) -> ast.expr | None:
    """The name a lookup looks up: its first argument, or its cmd or name keyword."""
    if call.args:
        return call.args[0]
    return next((item.value for item in call.keywords if item.arg in LOOKUP_KEYWORDS), None)


def _program_name(command: str) -> str | None:
    """The program a command string starts, by its first word and without a .exe suffix, or None for a path or a word
    that names no program."""
    words = command.split(None, 1)
    name = words[0] if words else ""
    name = platform_support.program_name(name)
    return name if COMMAND_NAME.fullmatch(name) else None


# A word that names a program by a path: one holding a separator, or a Windows drive.
PATH_WORD = re.compile(r"[^/\\]*[/\\].*|[A-Za-z]:.*")


def _read_literal(source: ast.Constant) -> Source:
    """What a string literal a command starts names: the literal itself when its first word names a program, the
    executable's base name, without a .exe suffix, when the whole literal is one path to it, and Unreadable for a path
    spaces or arguments follow, which could be part of the path or not, or for a literal that names no program."""
    value = str(source.value)
    if _program_name(value):
        return source
    words = value.split()
    if not words or not PATH_WORD.fullmatch(words[0]):
        return Unreadable(source, f"the program in `{ast.unparse(source)}`, which names none")
    if len(words) > 1:
        return Unreadable(source, f"the program in `{ast.unparse(source)}`, a path that a space may be part of")
    name = "" if words[0][-1] in "/\\" else platform_support.program_name(PureWindowsPath(words[0]).name)
    if not COMMAND_NAME.fullmatch(name):
        return Unreadable(source, f"the program in `{ast.unparse(source)}`, a path that names none")
    return ast.copy_location(ast.Constant(name), source)


def _read_literals(sources: list[Source]) -> list[Source]:
    return [_read_literal(source) if isinstance(source, ast.Constant) else source for source in sources]


def _type_uses(tree: ast.Module, resolver: CallableResolver) -> set[int]:
    """The nodes that use a class as a type, which starts nothing: an annotation, a subscripted generic such as
    subprocess.Popen[bytes], and the class isinstance or issubclass checks against."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.Subscript):
            roots.append(node.value)
        elif isinstance(node, ast.Call) and resolver.name(node.func) in {"isinstance", "issubclass"}:
            roots += node.args[1:2]
    return {id(found) for root in roots for found in ast.walk(root)}


class _NameBindings:
    """Every value the module binds each name to, in the lexical scope that owns the name: an assigned value, each
    element of what a loop or a comprehension walks, or None for a store whose value the reader cannot read, such as
    a with statement's target, an import, or an unpacking it cannot pair. A function's parameters are read through
    its calls instead."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver) -> None:
        self.scopes = resolver.scopes
        self.values: dict[tuple[int, str], list[Binding]] = defaultdict(list)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self._bind(target, node.value, node)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                self._bind(node.target, node.value, node)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                element = _Element(node.iter) if isinstance(node.target, ast.Name) else None
                self._bind(node.target, element, node.target)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                self._bind(node.optional_vars, None, node.optional_vars)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.values[self.key(node, (alias.asname or alias.name).split(".")[0])].append(None)

    def key(self, node: ast.AST, name: str) -> tuple[int, str]:
        """The scope that owns a name where node uses or stores it, and the name."""
        scope = self.scopes.get(id(node))
        owner = scope.owner(name) if scope is not None else None
        return (id(owner) if owner is not None else 0, name)

    def _bind(self, target: ast.expr, value: Binding, anchor: ast.AST) -> None:
        """Bind each name a target stores: a name to value, and each name an unpacking stores to the item it pairs
        with, or to None when it cannot pair them. A walrus target is placed by its expression, its anchor."""
        if isinstance(target, ast.Starred):
            self._bind(target.value, None, anchor)
        elif isinstance(target, ast.Name):
            self.values[self.key(target if id(target) in self.scopes else anchor, target.id)].append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
            paired = len(items) == len(target.elts) and not any(isinstance(item, ast.Starred) for item in target.elts)
            for index, item in enumerate(target.elts):
                self._bind(item, items[index] if paired else None, anchor)

    def of(self, node: ast.Name) -> tuple[tuple[int, str], list[Binding]]:
        """The key of the name a node uses, and every value its scope binds it to."""
        key = self.key(node, node.id)
        return key, self.values.get(key, [])


def _scope_names(tree: ast.Module) -> dict[int, str]:
    """The scope holding each node: the innermost function or class holding it, by qualified name, or else the
    module-level assignment holding it, by its target, or else the module."""
    scopes: dict[int, str] = {}

    def visit(node: ast.AST, scope: str) -> None:
        scopes[id(node)] = scope
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, child.name if scope == MODULE_SCOPE else f"{scope}.{child.name}")
            else:
                visit(child, scope)

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visit(statement, statement.name)
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        named = [target.id for target in targets if isinstance(target, ast.Name)]
        visit(statement, named[0] if len(targets) == len(named) == 1 else MODULE_SCOPE)
    return scopes


def _own_nodes(function: Function) -> list[ast.AST]:
    """The nodes of a function's body, without those of the functions and classes it defines."""
    pending: list[ast.AST] = [*function.body] if isinstance(function.body, list) else [function.body]
    found: list[ast.AST] = []
    while pending:
        node = pending.pop()
        found.append(node)
        pending += [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        ]
    return found


def _calls(call: ast.Call, name: str) -> bool:
    """Whether a call may call the function of that name: by the name itself, or as a method of anything."""
    function = call.func
    return (isinstance(function, ast.Name) and function.id == name) or (
        isinstance(function, ast.Attribute) and function.attr == name
    )


def _argument(function: Function, parameter: ast.arg, call: ast.Call) -> ast.expr | Unreadable | None:
    """What a call passes a parameter: its argument, its default when it passes none, None when it has neither, or
    Unreadable when an unpacked argument may hold it."""
    arguments = function.args
    keyword = next((item.value for item in call.keywords if item.arg == parameter.arg), None)
    if keyword is not None:
        return keyword
    positional = [*arguments.posonlyargs, *arguments.args]
    bound = isinstance(call.func, ast.Attribute) and bool(positional) and positional[0].arg in {"self", "cls"}
    unpacked = any(isinstance(item, ast.Starred) for item in call.args) or any(
        item.arg is None for item in call.keywords
    )
    if parameter in positional:
        index = positional.index(parameter) - bound
        if 0 <= index < len(call.args) and not unpacked:
            return call.args[index]
    if unpacked:
        return Unreadable(call, f"the program in `{ast.unparse(call)}`, which unpacks its arguments")
    if parameter in positional:
        offset = positional.index(parameter) - (len(positional) - len(arguments.defaults))
        return arguments.defaults[offset] if offset >= 0 else None
    return arguments.kw_defaults[arguments.kwonlyargs.index(parameter)]


def _prefixes(sources: list[Source], placeholder: str, kind: str) -> list[Source]:
    """Each string's literal prefix before its first placeholder, or Unreadable where that prefix names no program."""
    found: list[Source] = []
    for source in sources:
        if isinstance(source, ast.Constant) and isinstance(source.value, str) and placeholder in source.value:
            prefix = source.value.split(placeholder, 1)[0]
            if _program_name(prefix):
                found.append(ast.copy_location(ast.Constant(prefix), source))
            else:
                found.append(Unreadable(source, f"{kind} command without a literal program prefix"))
        else:
            found.append(source)
    return found


class CommandReader:
    """The programs a Python module runs, each call resolved through the module's imports and each command it passes
    followed to what names its program: a literal the module binds the name to, a lookup, or, for a parameter, what
    each call of its function in the module passes. A literal path names its program by its base name. Where none can
    be read, the reader says so, as it does for a runner the module hands on as a value or takes by getattr, so a
    command whose program it cannot read never passes unseen.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.resolver = CallableResolver(tree)
        self.bindings = _NameBindings(tree, self.resolver)
        self.scope_names = _scope_names(tree)
        self.calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.called = {id(call.func) for call in self.calls}
        # The values the module binds a name to, which a call through that name is resolved by.
        self.alias_values = {
            id(value)
            for scope in {id(scope): scope for scope in self.resolver.scopes.values()}.values()
            for values in scope.bindings.values()
            for value in values
            if isinstance(value, ast.expr)
        }
        # The names the module uses other than by calling them, such as a function it hands on as a value.
        self.handed_on = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)) or isinstance(node, ast.Attribute)
            if id(node) not in self.called
        }
        # The functions other modules may call: the module's own and its classes' methods, by a public name.
        self.public = {
            id(definition)
            for statement in tree.body
            for definition in ([statement] if not isinstance(statement, ast.ClassDef) else statement.body)
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) and not definition.name.startswith("_")
        }
        self.functions: dict[str, list[Function]] = defaultdict(list)
        self.parameters: dict[tuple[int, str], tuple[Function, ast.arg]] = {}
        self.parameter_functions: dict[int, Function] = {}
        self.attributes: dict[str, list[ast.expr]] = defaultdict(list)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self._read_function(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Attribute):
                        self.attributes[target.attr].append(node.value)
        self._commands: dict[frozenset[str], set[str]] = {}
        self._problems: dict[str, list[str]] = {}
        self._unreadable: list[Unreadable] | None = None

    def _read_function(self, function: Function) -> None:
        if not isinstance(function, ast.Lambda):
            self.functions[function.name].append(function)
        arguments = function.args
        variadic = [argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None]
        for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, *variadic]:
            self.parameters[(id(self.resolver.scopes[id(argument)]), argument.arg)] = (function, argument)
            self.parameter_functions[id(argument)] = function

    def program_literals(self, node: ast.expr | None, seen: frozenset[object] = frozenset()) -> list[Source]:
        """What names the program a command expression runs: a string, the first item of a list or tuple, the left
        side of a +, the literal prefix of an f-string or of a % or .format() string, what a join, a split, a path
        conversion, or a lookup is given, what one of the module's own functions returns, every value a name is bound
        to, and, for a parameter, what every call of its function passes it. Anything else is Unreadable, and so is an
        f-string or a format whose prefix names no program."""
        if node is None:
            return []
        if isinstance(node, ast.Constant):
            return [node] if isinstance(node.value, str) else []
        if isinstance(node, ast.JoinedStr):
            first = node.values[0] if node.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and _program_name(first.value):
                return [first]
            return [Unreadable(node, "f-string command without a literal program prefix")]
        if isinstance(node, (ast.List, ast.Tuple)):
            if node.elts:
                return self.program_literals(node.elts[0], seen)
            return [Unreadable(node, f"the program in `{ast.unparse(node)}`, which the module fills in elsewhere")]
        if isinstance(node, (ast.Starred, ast.NamedExpr, ast.Await)):
            return self.program_literals(node.value, seen)
        if isinstance(node, ast.IfExp):
            return self.program_literals(node.body, seen) + self.program_literals(node.orelse, seen)
        if isinstance(node, ast.BoolOp):
            return [found for value in node.values for found in self.program_literals(value, seen)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            empty = isinstance(node.left, (ast.List, ast.Tuple)) and not node.left.elts
            return self.program_literals(node.right if empty else node.left, seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return _prefixes(self.program_literals(node.left, seen), "%", "%-format")
        if isinstance(node, ast.Call):
            return self._call_sources(node, seen)
        if isinstance(node, ast.Name):
            return self._name_sources(node, seen)
        if isinstance(node, ast.Attribute):
            return self._attribute_sources(node, seen)
        return [Unreadable(node, f"the program in `{ast.unparse(node)}`")]

    def _call_sources(self, call: ast.Call, seen: frozenset[object]) -> list[Source]:
        function = call.func
        if _is_lookup(call, self.resolver.aliases(function)):
            return self.program_literals(_lookup_argument(call), seen)
        if self.resolver.name(function) in PROGRAM_CONVERSIONS and call.args:
            return self.program_literals(call.args[0], seen)
        if isinstance(function, ast.Attribute):
            receiver = function.value
            if function.attr in PATH_METHODS:
                return self.program_literals(receiver, seen)
            if function.attr == "format":
                return _prefixes(self.program_literals(receiver, seen), "{", ".format()")
            if function.attr == "join" and isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
                return self.program_literals(call.args[0] if call.args else None, seen)
        return self._returned(call, seen)

    def _returned(self, call: ast.Call, seen: frozenset[object]) -> list[Source]:
        """What a call of one of the module's own functions returns, or Unreadable for any other call."""
        function = call.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        own = not isinstance(function, ast.Name) or self.resolver.name(function) == f"<local>.{name}"
        definitions = self.functions.get(name, []) if own else []
        if not definitions:
            return [Unreadable(call, f"the program in `{ast.unparse(call)}`")]
        found: list[Source] = []
        for definition in definitions:
            if ("returns", id(definition)) in seen:
                continue
            inner = seen | {("returns", id(definition))}
            values = [node.value for node in _own_nodes(definition) if isinstance(node, ast.Return) and node.value]
            if not values:
                found.append(Unreadable(call, f"the program in `{ast.unparse(call)}`, whose function returns none"))
            found += [source for value in values for source in self.program_literals(value, inner)]
        return found

    def _name_sources(self, node: ast.Name, seen: frozenset[object]) -> list[Source]:
        key, values = self.bindings.of(node)
        if ("name", key) in seen:
            return []
        inner = seen | {("name", key)}
        found: list[Source] = []
        for value in values:
            if isinstance(value, _Element):
                found += self._element_sources(value.collection, inner)
            elif value is not None:
                found += self.program_literals(value, inner)
        parameter = self.parameters.get(key)
        if parameter is not None:
            found += self._argument_sources(*parameter, inner)
        elif not values or None in values:
            found.append(Unreadable(node, f"the program in `{node.id}`, which the module binds to nothing it reads"))
        return found

    def _element_sources(self, collection: ast.expr, seen: frozenset[object]) -> list[Source]:
        """What names the program of each element of a collection a loop or a comprehension walks."""
        if isinstance(collection, (ast.List, ast.Tuple, ast.Set)):
            return [found for item in collection.elts for found in self.program_literals(item, seen)]
        key, values = self.bindings.of(collection) if isinstance(collection, ast.Name) else ((0, ""), [])
        if values and all(isinstance(value, (ast.List, ast.Tuple, ast.Set)) for value in values):
            if ("elements", key) in seen:
                return []
            inner = seen | {("elements", key)}
            return [
                found
                for value in values
                if isinstance(value, (ast.List, ast.Tuple, ast.Set))
                for found in self._element_sources(value, inner)
            ]
        return [Unreadable(collection, f"the program in each item of `{ast.unparse(collection)}`")]

    def _attribute_sources(self, node: ast.Attribute, seen: frozenset[object]) -> list[Source]:
        """The interpreter for sys.executable, or every value the module assigns an attribute of that name."""
        if self.resolver.name(node) == INTERPRETER:
            return [ast.copy_location(ast.Constant(INTERPRETER_PROGRAM), node)]
        values = self.attributes.get(node.attr, [])
        if not values:
            return [Unreadable(node, f"the program in `{ast.unparse(node)}`")]
        if ("attribute", node.attr) in seen:
            return []
        inner = seen | {("attribute", node.attr)}
        return [found for value in values for found in self.program_literals(value, inner)]

    def _argument_sources(self, function: Function, parameter: ast.arg, seen: frozenset[object]) -> list[Source]:
        """What every call of a function in the module passes a parameter that names a program, and Unreadable when
        the module may not hold every call: none calls it, it hands the function on as a value, other modules may call
        it by its public name, or the parameter is variadic."""
        name = "" if isinstance(function, ast.Lambda) else function.name
        variadic = parameter in (function.args.vararg, function.args.kwarg)
        calls = [call for call in self.calls if _calls(call, name)] if name and not variadic else []
        if not calls:
            return [
                Unreadable(parameter, f"the program in parameter {parameter.arg}, which no call in this module passes")
            ]
        found: list[Source] = []
        if name in self.handed_on:
            found.append(
                Unreadable(parameter, f"the program in parameter {parameter.arg}, whose function is passed on")
            )
        elif id(function) in self.public:
            found.append(
                Unreadable(parameter, f"the program in parameter {parameter.arg}, which other modules may pass")
            )
        if ("parameter", id(parameter)) in seen:
            return found
        inner = seen | {("parameter", id(parameter))}
        for call in calls:
            argument = _argument(function, parameter, call)
            found += [argument] if isinstance(argument, Unreadable) else self.program_literals(argument, inner)
        return found

    def _command_arguments(self, call: ast.Call) -> list[ast.expr | Unreadable]:
        """The argument of a COMMAND_RUNNERS call that names the program it starts, its executable= when it gives one,
        since that replaces the program and leaves the command's first item only the name the program is told, and
        nothing for another call."""
        name = self.resolver.name(call.func)
        runner = COMMAND_RUNNERS.get(name)
        if runner is None:
            return []
        executable = next((item.value for item in call.keywords if item.arg == "executable"), None)
        if executable is not None and name in EXECUTABLE_RUNNERS:
            return [executable]
        position, keyword = runner
        starred = any(isinstance(item, ast.Starred) for item in call.args[: position + 1])
        given = next((item.value for item in call.keywords if item.arg == keyword), None)
        if len(call.args) > position and not starred:
            return [call.args[position]]
        if given is not None:
            return [given]
        if starred or any(item.arg is None for item in call.keywords):
            return [Unreadable(call, f"the program in `{ast.unparse(call)}`, which unpacks its arguments")]
        return []

    def _sources(self, call: ast.Call) -> list[Source]:
        return _read_literals(
            [
                found
                for argument in self._command_arguments(call)
                for found in ([argument] if isinstance(argument, Unreadable) else self.program_literals(argument))
            ]
        )

    def _names_a_runner(self, node: ast.expr, seen: frozenset[int] = frozenset()) -> bool:
        """Whether an expression names a COMMAND_RUNNERS call, through the module's imports or any value the module
        binds the name to, even where a name bound more than once does not resolve."""
        if self.resolver.name(node) in COMMAND_RUNNERS:
            return True
        scope = self.resolver.scopes.get(id(node))
        if not isinstance(node, ast.Name) or scope is None or id(node) in seen:
            return False
        owner = scope.owner(node.id)
        values = owner.bindings.get(node.id, []) if owner is not None else []
        return any(
            isinstance(value, (ast.Name, ast.Attribute)) and self._names_a_runner(value, seen | {id(node)})
            for value in values
        )

    def _runners_handed_on(self) -> list[Unreadable]:
        """Each runner the module uses other than by calling it or binding a name to it, whose commands the reader
        cannot follow: one handed to functools.partial, map, a Thread's target, or a collection, or taken by getattr."""
        found: list[Unreadable] = []
        types = _type_uses(self.tree, self.resolver)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and self.resolver.name(node.func) == "getattr" and len(node.args) > 1:
                module = self.resolver.name(node.args[0])
                attribute = node.args[1]
                named = (
                    isinstance(attribute, ast.Constant)
                    and isinstance(attribute.value, str)
                    and f"{module}.{attribute.value}" in COMMAND_RUNNERS
                )
                dynamic = not isinstance(attribute, ast.Constant) and any(
                    runner.startswith(f"{module}.") for runner in COMMAND_RUNNERS
                )
                if module and (named or dynamic):
                    found.append(Unreadable(node, f"the program `{ast.unparse(node)}` runs, which getattr takes"))
            elif (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
                and id(node) not in self.called
                and id(node) not in self.alias_values
                and id(node) not in types
                and self._names_a_runner(node)
            ):
                found.append(
                    Unreadable(node, f"the program `{ast.unparse(node)}` runs, which the module hands on as a value")
                )
        return found

    @staticmethod
    def _named(sources: list[Source]) -> list[tuple[int, str]]:
        return [
            (source.lineno, name)
            for source in sources
            if isinstance(source, ast.Constant)
            and isinstance(source.value, str)
            and (name := _program_name(source.value))
        ]

    def started(self, call: ast.Call) -> list[tuple[int, str]]:
        """The line and program of each command a COMMAND_RUNNERS call may start, and nothing for another call."""
        return self._named(self._sources(call))

    def starts_process(self) -> bool:
        """Whether the module calls a command runner or a skill-core client, even with a dynamic command.

        An import, command lookup, or command list alone starts nothing. This does not say whether the process
        runs code from the target repository; that is the script's RUNS_REPOSITORY_CODE declaration.
        """
        return bool(self._runners_handed_on()) or any(
            self.resolver.name(node.func) in COMMAND_RUNNERS or self.resolver.name(node.func) in CLIENT_CLASSES
            for node in self.calls
        )

    def scope(self, source: Unreadable) -> str:
        """The function, class, or module-level assignment holding what cannot be read, as COMMANDS_ALLOWED names it."""
        return self.scope_names.get(id(source.node), MODULE_SCOPE)

    def unreadable(self) -> list[Unreadable]:
        """Each place a command the module starts names a program the reader cannot read, once, in line order."""
        if self._unreadable is None:
            started = [source for call in self.calls for source in self._sources(call)]
            found = {
                (source.line, source.description, self.scope(source)): source
                for source in [*started, *self._runners_handed_on()]
                if isinstance(source, Unreadable)
            }
            self._unreadable = [found[key] for key in sorted(found)]
        return self._unreadable

    def declared_runner(self, source: Unreadable, module: str) -> bool:
        """Whether what cannot be read is the command parameter of a function COMMAND_RUNNERS names, whose command is
        read instead at every call of it."""
        function = self.parameter_functions.get(id(source.node))
        if function is None or isinstance(function, ast.Lambda) or id(function) not in self.public:
            return False
        runner = COMMAND_RUNNERS.get(f"{module}.{function.name}")
        positional = [*function.args.posonlyargs, *function.args.args]
        return runner is not None and positional[runner[0] : runner[0] + 1] == [source.node]

    def problems(self, path: str) -> list[str]:
        """Each call through an alias that cannot be resolved, and each command whose program cannot be read where the
        module's COMMANDS_ALLOWED does not state why, then each allowance the module no longer needs."""
        if path not in self._problems:
            self._problems[path] = self._read_problems(path)
        return list(self._problems[path])

    def _read_problems(self, path: str) -> list[str]:
        problems = self.resolver.problems(path)
        allowed = module_allowance(self.tree, COMMANDS_ALLOWANCE)
        if allowed is None:
            problems.append(f"{path}: {COMMANDS_ALLOWANCE} must map each scope to the reason its program is unread")
            allowed = {}
        module = PurePosixPath(path).stem
        found = [source for source in self.unreadable() if not self.declared_runner(source, module)]
        problems += [
            f"{path}:{source.line} cannot read {source.description}; name the program with a literal or a lookup, "
            f"or state why in {COMMANDS_ALLOWANCE}; see {COMMANDS_DOC}"
            for source in found
            if self.scope(source) not in allowed
        ]
        problems += [
            f"{path}: {COMMANDS_ALLOWANCE} allows {scope}, which starts no command whose program is unread"
            for scope in sorted(set(allowed) - {self.scope(source) for source in found})
        ]
        return problems

    def commands(self, known: set[str]) -> set[str]:
        """Every program the module runs through a runner or a skill-core client, or looks up with which, and every
        known tool a list or tuple begins with, since a module may hand that command to a function of its own."""
        key = frozenset(known)
        if key not in self._commands:
            found: set[str] = set()
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                    first = node.elts[0]
                    if isinstance(first, ast.Constant) and first.value in known:
                        found.add(first.value)
                elif isinstance(node, ast.Call):
                    client = CLIENT_CLASSES.get(self.resolver.name(node.func))
                    found |= {client} if client else set()
                    if _is_lookup(node, self.resolver.aliases(node.func)):
                        looked_up = _read_literals(self.program_literals(_lookup_argument(node)))
                        found |= {name for _, name in self._named(looked_up)}
                    found |= {name for _, name in self.started(node)}
            self._commands[key] = found
        return self._commands[key]


@functools.cache
def _read_commands(source: str) -> CommandReader:
    return CommandReader(ast.parse(source))


def command_reader(path: Path) -> CommandReader:
    """The CommandReader of a Python file as it is now, built once for each content in a validation run, so every
    policy that reads a file's commands shares one reading of it and a file that changes is read again."""
    return _read_commands(path.read_text(encoding="utf-8", errors="replace"))


# Prints one JSON array of {source, name} for each external command run by the PowerShell sources in the JSON file
# COMMAND_SOURCES names, as PowerShell's own parser reads them. A function a source defines is its own, and so is
# what PowerShell ships: its aliases and the commands of its core and of the modules under its home, such as
# Get-ChildItem and ForEach-Object. A module installed elsewhere, such as PSScriptAnalyzer, is not standard.
POWERSHELL_COMMANDS_RUN = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$sources = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$shipped = @(Get-Module -ListAvailable | Where-Object { "
    "$_.ModuleBase.StartsWith($PSHOME, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object Name); "
    "$own = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); "
    "foreach ($command in Get-Command -Module @($shipped + 'Microsoft.PowerShell.Core')) { "
    "[void]$own.Add($command.Name) }; "
    "foreach ($alias in Get-Alias) { [void]$own.Add($alias.Name) }; "
    "$found = [Collections.Generic.List[object]]::new(); "
    "for ($index = 0; $index -lt $sources.Count; $index++) { "
    "$tokens = $null; $errors = $null; "
    "$tree = [Management.Automation.Language.Parser]::ParseInput($sources[$index], [ref]$tokens, [ref]$errors); "
    "$functions = @($tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name); "
    "foreach ($command in $tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.CommandAst] }, $true)) { "
    "$name = $command.GetCommandName(); "
    "if (-not $name -or $functions -contains $name -or $own.Contains($name)) { continue }; "
    "$found.Add([ordered]@{ source = $index; name = $name }) } }; "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $found.ToArray() -Compress -Depth 3))"
)


def powershell_commands(sources: list[str]) -> list[set[str]]:
    """The external programs each PowerShell source runs, read by PowerShell's own parser in one call for them all."""
    if not sources:
        return []
    with tempfile.TemporaryDirectory(prefix="powershell-commands-") as directory:
        request = Path(directory) / "sources.json"
        request.write_text(json.dumps(sources), encoding="utf-8")
        result = platform_support.run_tool(
            [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_COMMANDS_RUN],
            {"COMMAND_SOURCES": str(request)},
        )
    lines = [line for line in result.output.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if result.returncode == 0 and lines else None
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, list):
        raise AssertionError(
            f"PowerShell could not read the commands (exit code {result.returncode}):\n{result.output}"
        )
    commands: list[set[str]] = [set() for _ in sources]
    for item in found:
        if name := _program_name(item["name"]):
            commands[item["source"]].add(name)
    return commands


def _powershell_sources(path: Path) -> list[str]:
    """The PowerShell a file holds: a .ps1 script, or Markdown's PowerShell fences."""
    suffix = path.suffix.casefold()
    if suffix == ".ps1":
        return [path.read_text(encoding="utf-8-sig", errors="replace")]
    if suffix == ".md":
        return _fences(path.read_text(encoding="utf-8", errors="replace"), "powershell")
    return []


def _file_commands(path: Path, known: set[str], powershell: dict[str, set[str]]) -> set[str]:
    """The programs a file runs, where powershell holds what each PowerShell source read for the run runs."""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    found = {command for source in _powershell_sources(path) for command in powershell[source]}
    if suffix == ".py":
        found |= command_reader(path).commands(known)
    elif suffix in {".sh", ".bash"}:
        found |= shell_commands(text)
    elif suffix == ".md":
        found |= {command for block in _fences(text, "shell") for command in shell_commands(block)}
    return found


def _skill_files(directory: Path) -> list[Path]:
    """A skill's files, without its tests."""
    return sorted(path for path in directory.rglob("*") if path.is_file() and not is_test_script(path))


NAMED_SCRIPT = re.compile(r"/([a-z0-9-]+)/scripts/([A-Za-z_]\w*)\.py\b")


def _referenced_scripts(path: Path) -> frozenset[str]:
    """Module names a file imports, and script names it gives by a path through another skill's scripts/."""
    return _references(path.read_text(encoding="utf-8", errors="replace"), path.suffix.casefold() == ".py")


@functools.cache
def _references(text: str, python: bool) -> frozenset[str]:
    """What _referenced_scripts finds in a file's text, read once for each content in a validation run."""
    if not python:
        return frozenset(name for _, name in NAMED_SCRIPT.findall(text))
    tree = ast.parse(text)
    # A module-level sanction such as DUPLICATION_ALLOWED names, in its reasons, the other files that hold a copy,
    # which the module does not run.
    sanctions = {
        line
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id.endswith("_ALLOWED") for target in node.targets)
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    unsanctioned = "".join(
        line for number, line in enumerate(text.splitlines(keepends=True), 1) if number not in sanctions
    )
    return frozenset({name for _, name in NAMED_SCRIPT.findall(unsanctioned)}) | {
        found.module.split(".")[0] for found in module_imports(tree)
    }


def _skill_dependencies(root: Path, skill: str) -> list[str]:
    """The skill_deps closure of a skill, from deploy-meta."""
    found: list[str] = []
    pending = [skill]
    while pending:
        metadata = root / "deploy-meta" / f"{pending.pop()}.json"
        if not metadata.is_file():
            continue
        for dependency in json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []):
            if dependency not in found and dependency != skill:
                found.append(dependency)
                pending.append(dependency)
    return found


def _reached_dependency_scripts(root: Path, skill: str, own: list[Path]) -> list[Path]:
    """The dependency scripts a skill runs: those its own files import or name, and what those import in turn."""
    scripts: dict[str, Path] = {}
    skills = skills_by_name(root)
    for dependency in _skill_dependencies(root, skill):
        found = sorted((skills[dependency] / "scripts").glob("*.py")) if dependency in skills else []
        for path in found:
            if not is_test_script(path):
                scripts.setdefault(path.stem, path)
    own_modules = {path.stem for path in own if path.suffix.casefold() == ".py"}
    pending = sorted({name for path in own for name in _referenced_scripts(path)} - own_modules)
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached or name not in scripts:
            continue
        reached.add(name)
        pending.extend(_referenced_scripts(scripts[name]))
    return [scripts[name] for name in sorted(reached)]


def skill_command_problems(root: Path, known: set[str], standard: set[str]) -> list[str]:
    """Report commands a skill runs that are neither standard nor declared, and declared tools it never runs.

    A skill runs what its own scripts and Bash examples run, and what the scripts of its skill_deps that it reaches
    run: a dependency's tools are not the skill's unless it imports or names the script that runs them, and then the
    skill declares them too, so a skill that reaches skill-core's GitHub client declares gh.
    """
    skills: list[tuple[str, set[str], list[Path], list[Path]]] = []
    directories = skills_by_name(root)
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        document = json.loads(metadata.read_text(encoding="utf-8"))
        declared = set(document.get("tools", [])) | set(document.get("optional_tools", []))
        own = _skill_files(directories[metadata.stem]) if metadata.stem in directories else []
        skills.append((metadata.stem, declared, own, _reached_dependency_scripts(root, metadata.stem, own)))
    # PowerShell reads every source in one start, which costs far more than reading one.
    sources = sorted(
        {source for *_, own, deps in skills for path in own + deps for source in _powershell_sources(path)}
    )
    powershell = dict(zip(sources, powershell_commands(sources), strict=True))
    problems: list[str] = []
    for skill, declared, own, dependency_scripts in skills:
        for path in own + dependency_scripts:
            if path.suffix.casefold() == ".py":
                problems += command_reader(path).problems(path.relative_to(root).as_posix())
        used = {command for path in own for command in _file_commands(path, known, powershell)}
        reached: dict[str, Path] = {}
        for path in dependency_scripts:
            for command in _file_commands(path, known, powershell):
                reached.setdefault(command, path)
        for name in sorted((used & known) - declared):
            problems.append(f"skill {skill} runs {name} without declaring it in tools")
        for name in sorted((set(reached) & known) - used - declared):
            through = reached[name].relative_to(root).as_posix()
            problems.append(f"skill {skill} runs {name} through {through} without declaring it in tools")
        for name in sorted(declared - used - set(reached)):
            problems.append(f"skill {skill} declares tool {name} but never runs it")
        for name in sorted(used - known - standard - SHELL_BUILTINS):
            problems.append(f"skill {skill} runs {name}, which a standard install lacks; see {COMMANDS_DOC}")
    return problems


GH_FILTERS = {"--jq", "--template"}
GH_SHELL_CALL = re.compile(r"(?:^|[\s;|&(])gh\s")
GH_SHELL_FILTER = re.compile(r"\s(--jq|--template|-q)(?=[\s=]|$)")


def gh_filter_problems(root: Path) -> list[str]:
    """Report skill scripts that filter gh output with --jq, -q, or --template instead of parsing its JSON.

    Python parsing has one shape check that fails closed and can be tested offline from a fixture; a jq expression
    is a second language that only gh itself can run.
    """
    problems: list[str] = []
    for path in sorted((root / "skills").glob("**/scripts/*")):
        if not path.is_file() or is_test_script(path):
            continue
        name = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        found: list[tuple[int, str]] = []
        if path.suffix.casefold() == ".py":
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Constant) and node.value in GH_FILTERS:
                    found.append((node.lineno, node.value))
                elif isinstance(node, (ast.List, ast.Tuple)):
                    values = {item.value for item in node.elts if isinstance(item, ast.Constant)}
                    # gh's -q applies a jq filter to --json output; git's -q never comes with --json.
                    if {"-q", "--json"} <= values:
                        found.append((node.lineno, "-q"))
        elif path.suffix.casefold() in {".sh", ".bash", ".ps1"}:
            for number, line in enumerate(text.splitlines(), start=1):
                match = GH_SHELL_FILTER.search(line) if GH_SHELL_CALL.search(line) else None
                if match:
                    found.append((number, match.group(1)))
        for number, flag in sorted(set(found)):
            problems.append(
                f"{name}:{number} filters gh output with {flag}; parse its JSON in Python instead; see {COMMANDS_DOC}"
            )
    return problems


CONSOLE_CORE = "skill-core"
CONSOLE_RUNNER = "run_main"
CONSOLE_FUNCTION = f"console.{CONSOLE_RUNNER}"
CONSOLE_DOC = '"Script results" in docs/adding-a-skill.md'


def console_entry_points(root: Path) -> list[Path]:
    """The Python that can run as a program: every shipped and repository skill's scripts outside skill-core, deploy.py,
    the deployer, and tools/, regression suites aside."""
    files = [
        path
        for scripts in skill_script_directories(root)
        if scripts.parent.name != CONSOLE_CORE
        for path in scripts.glob("*.py")
    ]
    files += [root / "deploy.py", *(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    return sorted(
        (path for path in files if path.is_file() and not is_test_script(path)),
        key=lambda path: path.relative_to(root).as_posix(),
    )


# The standard streams run_main configures, under every name sys gives them.
STANDARD_STREAMS = frozenset(
    f"sys.{name}" for stream in ("stdin", "stdout", "stderr") for name in (stream, f"__{stream}__")
)
# Calls that build a text stream over a binary stream or a file descriptor, choosing its encoding, and the codecs
# calls that return a class that does.
STREAM_WRAPPERS = frozenset({"io.TextIOWrapper", "open", "builtins.open", "io.open", "os.fdopen"})
CODEC_WRAPPERS = frozenset({"codecs.getwriter", "codecs.getreader"})


def _is_reconfigure(node: ast.expr) -> bool:
    """Whether an expression is a stream's reconfigure method: stream.reconfigure, or getattr(stream, "reconfigure")."""
    if isinstance(node, ast.Attribute):
        return node.attr == "reconfigure"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "reconfigure"
    )


def _reconfigure_names(tree: ast.Module) -> set[str]:
    """The names a module binds to a stream's reconfigure method anywhere in it, and reconfigure itself."""
    names = {"reconfigure"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_reconfigure(node.value):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value and _is_reconfigure(node.value):
            names |= {node.target.id} if isinstance(node.target, ast.Name) else set()
    return names


def _names_a_standard_stream(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Whether an expression reaches a standard stream, such as sys.stdout.buffer or sys.stderr.fileno()."""
    return any(qualified_name(part, aliases) in STANDARD_STREAMS for part in ast.walk(node))


def _changes_a_stream(node: ast.AST, aliases: dict[str, str], reconfigures: set[str]) -> bool:
    """Whether a node sets a stream's encoding: a reconfigure call that may pass one, directly or through a name bound
    to the method; an assignment or setattr that replaces a standard stream; or a text wrapper built over one."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(
            isinstance(target, ast.Attribute) and qualified_name(target, aliases) in STANDARD_STREAMS
            for target in targets
        )
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if _is_reconfigure(function) or (isinstance(function, ast.Name) and function.id in reconfigures):
        return any(keyword.arg in {"encoding", None} for keyword in node.keywords)
    if qualified_name(function, aliases) in {"setattr", "builtins.setattr"} and len(node.args) > 1:
        stream = node.args[1]
        return (
            qualified_name(node.args[0], aliases) == "sys"
            and isinstance(stream, ast.Constant)
            and isinstance(stream.value, str)
            and f"sys.{stream.value}" in STANDARD_STREAMS
        )
    wrapper = qualified_name(function, aliases) in STREAM_WRAPPERS or (
        isinstance(function, ast.Call) and qualified_name(function.func, aliases) in CODEC_WRAPPERS
    )
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    return wrapper and any(_names_a_standard_stream(argument, aliases) for argument in arguments)


def _stream_change_lines(tree: ast.Module, aliases: dict[str, str]) -> list[int]:
    """The lines on which a module sets a standard stream's encoding itself."""
    reconfigures = _reconfigure_names(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.stmt, ast.expr)) and _changes_a_stream(node, aliases, reconfigures)
        }
    )


def _runs_through_the_runner(statement: ast.stmt, aliases: dict[str, str]) -> bool:
    """Whether a statement calls skill-core's run_main, resolved through the module's imports."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and qualified_name(statement.value.func, aliases) == CONSOLE_FUNCTION
    )


def console_setup_problems(root: Path) -> list[str]:
    """Report an entry point whose __main__ block does not start by calling skill-core's run_main(), and a module
    that reconfigures a stream's encoding itself.

    Skill scripts, deploy.py, the deployer, and tools/ print paths and text the user wrote, which a Windows pipe's
    legacy code page cannot encode, and promise a FAILED line, never a traceback, when they fail; one runner sets up
    the console and keeps that promise, so the copies cannot drift apart again.
    """
    problems: list[str] = []
    for path in console_entry_points(root):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = import_aliases(tree)
        problems += [
            f"{name}:{line} reconfigures a stream itself; call {CONSOLE_RUNNER}() from {CONSOLE_CORE} instead; "
            f"see {CONSOLE_DOC}"
            for line in _stream_change_lines(tree, aliases)
        ]
        for node in tree.body:
            if not (isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"):
                continue
            if not _runs_through_the_runner(node.body[0], aliases):
                problems.append(
                    f"{name}:{node.lineno} does not call {CONSOLE_CORE}'s {CONSOLE_RUNNER}() first in its __main__ "
                    f"block; see {CONSOLE_DOC}"
                )
    return problems


RESULTS_DOC = '"Script results" in docs/adding-a-skill.md'
CONTRACT_EXEMPTION = "EXIT_CONTRACT_EXEMPT"
CONTRACT_EXIT_CODES = {0, 1, 2}
SHELL_EXIT = re.compile(r"(?:^|[\s;&|(])exit\s+(\d+)\b")


def _contract_exemption(tree: ast.Module) -> str | None:
    """A module's EXIT_CONTRACT_EXEMPT reason, "" when it has none, or None when it is not a non-empty string."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == CONTRACT_EXEMPTION for target in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
                return value.value
            return None
    return ""


def _mentions_failed(node: ast.expr) -> bool:
    parts = node.values if isinstance(node, ast.JoinedStr) else [node]
    return any(
        isinstance(part, ast.Constant) and isinstance(part.value, str) and "FAILED" in part.value for part in parts
    )


def _contract_breaches(tree: ast.Module) -> list[tuple[int, str]]:
    """Where a script reports in a way "Script results" rules out, with what it does instead."""
    breaches: list[tuple[int, str]] = []
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    in_handler = {id(inner) for handler in handlers for statement in handler.body for inner in ast.walk(statement)}
    entry_points = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in {"main", "_main"}
    ]
    for entry_point in entry_points:
        for node in ast.walk(entry_point):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and type(node.value.value) is int
                and node.value.value not in CONTRACT_EXIT_CODES
            ):
                breaches.append((node.lineno, f"exits {node.value.value}; scripts exit only 0, 1, or 2"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "error"
            and id(node) in in_handler
            and isinstance(function.value, ast.Name)
            and "parser" in function.value.id
        ):
            breaches.append((node.lineno, "reports a failure through parser.error; print FAILED <reason> and exit 1"))
        exits = (
            isinstance(function, ast.Attribute)
            and function.attr == "exit"
            and isinstance(function.value, ast.Name)
            and function.value.id == "sys"
        )
        if (exits or (isinstance(function, ast.Name) and function.id == "SystemExit")) and node.args:
            code = node.args[0]
            if isinstance(code, ast.Constant) and type(code.value) is int and code.value not in CONTRACT_EXIT_CODES:
                breaches.append((node.lineno, f"exits {code.value}; scripts exit only 0, 1, or 2"))
        if isinstance(function, ast.Name) and function.id == "print" and node.args:
            stderr = any(
                keyword.arg == "file" and ast.unparse(keyword.value) == "sys.stderr" for keyword in node.keywords
            )
            if stderr and _mentions_failed(node.args[0]):
                breaches.append((node.lineno, "prints FAILED on stderr; print it on stdout"))
            first = node.args[0]
            if (
                not stderr
                and isinstance(first, ast.Call)
                and isinstance(first.func, ast.Attribute)
                and first.func.attr == "dumps"
                and ast.unparse(first.func.value) == "json"
            ):
                breaches.append((node.lineno, "prints JSON; print one fact per line"))
    return sorted(breaches)


# CodeQL's sensitive-data heuristic treats a call to a function whose name says "secret" or "trusted" (but not
# "untrusted" or "is_trusted") as returning a secret, and raises clear-text logging on wherever the result is printed.
# A commit SHA from a function named that way was flagged twice, so shipped code names such functions otherwise.
CODEQL_SECRET_NAME = re.compile(r"secret|(?<!un)(?<!un_)(?<!is)(?<!is_)trusted", re.IGNORECASE)


def secret_named_function_problems(root: Path) -> list[str]:
    """Report each function in shipped or deployer code whose name CodeQL reads as returning a secret."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    files += [path for scripts in skill_script_directories(root) for path in scripts.rglob("*.py")]
    problems: list[str] = []
    for path in sorted(
        (path for path in files if path.is_file() and not is_test_script(path)), key=lambda p: p.as_posix()
    ):
        name = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and CODEQL_SECRET_NAME.search(node.name):
                problems.append(
                    f"{name}:{node.lineno}: function {node.name} is named so CodeQL treats its result as a secret "
                    "and flags printing it; name it for what it returns"
                )
    return problems


def script_contract_problems(root: Path) -> list[str]:
    """Report skill scripts that break "Script results": a failure through parser.error or on stderr, JSON output,
    or an exit code other than 0, 1, and 2. A module that must speak another protocol says why in
    EXIT_CONTRACT_EXEMPT."""
    problems: list[str] = []
    for path in sorted(path for scripts in skill_script_directories(root) for path in scripts.glob("*")):
        if not path.is_file() or is_test_script(path):
            continue
        name = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            tree = ast.parse(text)
            exemption = _contract_exemption(tree)
            if exemption is None:
                problems.append(f"{name}: {CONTRACT_EXEMPTION} must be a non-empty string saying why")
            elif not exemption:
                problems += [f"{name}:{line} {breach}; see {RESULTS_DOC}" for line, breach in _contract_breaches(tree)]
        elif path.suffix in {".sh", ".bash"}:
            for number, line in enumerate(text.splitlines(), 1):
                for code in SHELL_EXIT.findall(line.split("#", 1)[0]):
                    if int(code) not in CONTRACT_EXIT_CODES:
                        problems.append(
                            f"{name}:{number} exits {code}; scripts exit only 0, 1, or 2; see {RESULTS_DOC}"
                        )
    return problems


# The command each shared client runs, and the client a skill script runs it through.
CLIENT_COMMANDS = {"git": "git_client.py's GitClient", "gh": "github_client.py's GitHubClient"}
CLIENTS_DOC = '"Script results" in docs/adding-a-skill.md'


def _client_commands(node: ast.AST, reader: CommandReader) -> list[tuple[int, str]]:
    """The line and the git or gh command a node starts: a list or tuple that begins with it, or a call that runs a
    command given it, such as `subprocess.run("git fetch", shell=True)`, directly or through a name bound to it."""
    found: list[tuple[int, str]] = []
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found = [(first.lineno, first.value.split(" ", 1)[0])]
    elif isinstance(node, ast.Call):
        found = reader.started(node)
    return [(line, command) for line, command in found if command in CLIENT_COMMANDS]


def client_command_problems(root: Path) -> list[str]:
    """Report a Python skill script outside skill-core that runs git or gh itself instead of through skill-core.

    The clients read no stdin, turn every prompt off, bound each command with a timeout, and classify its failures;
    a command a script starts itself has none of that, so a sweep can wait forever on a credential prompt.
    """
    problems: list[str] = []
    for path in sorted(path for scripts in skill_script_directories(root) for path in scripts.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        if is_test_script(path) or name.startswith(f"{SKILL_CORE_SCRIPTS}/"):
            continue
        reader = command_reader(path)
        problems += reader.problems(name)
        commands = {found for node in ast.walk(reader.tree) for found in _client_commands(node, reader)}
        for line, command in sorted(commands):
            problems.append(
                f"{name}:{line} runs {command} itself; run it through {SKILL_CORE}'s {CLIENT_COMMANDS[command]}, "
                f"which bounds it and turns prompts off; see {CLIENTS_DOC}"
            )
    return problems


class SkillScriptsPolicies(unittest.TestCase):
    def test_skill_scripts_follow_the_script_results_contract(self) -> None:
        # An agent that learned one script's results can read every other's.
        self.assertEqual([], script_contract_problems(REPOSITORY_ROOT))

    def test_no_shipped_function_is_named_so_codeql_reads_its_result_as_a_secret(self) -> None:
        self.assertEqual([], secret_named_function_problems(REPOSITORY_ROOT))

    def test_skills_run_only_standard_or_declared_commands(self) -> None:
        from deployer import tools

        self.assertEqual(
            [], skill_command_problems(REPOSITORY_ROOT, set(tools.SKILL_TOOLS), set(tools.STANDARD_COMMANDS))
        )

    def test_skill_scripts_parse_gh_json_instead_of_filtering_it(self) -> None:
        self.assertEqual([], gh_filter_problems(REPOSITORY_ROOT))

    def test_skill_scripts_run_git_and_gh_through_skill_core(self) -> None:
        self.assertEqual([], client_command_problems(REPOSITORY_ROOT))

    def test_entry_points_set_up_the_console_through_skill_core(self) -> None:
        self.assertEqual([], console_setup_problems(REPOSITORY_ROOT))

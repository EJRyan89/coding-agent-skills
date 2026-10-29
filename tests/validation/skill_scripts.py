"""What a skill's scripts may do: the commands they run, how they read gh output, how they run git and gh, the
"Script results" contract, the console setup, and the function names CodeQL reads as secrets.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import re
import shlex
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import git_client
import json_reader
import whole_number
from call_tables import EVENT_LOOP, PROCESS_STARTS
from duplication import SKILL_CORE_SCRIPTS
from markdown_sections import SHELL_FENCES
from path_containment import posix_within
from skill_roots import SKILL_CORE
from toolchain import powershell_json
from validation_support import (
    COMPUTED_NAME,
    MODULE_SCOPE,
    REPOSITORY_ROOT,
    CallableResolver,
    UnreadablePython,
    call_argument,
    find_fences,
    import_aliases,
    is_main_guard,
    is_test_script,
    module_allowance,
    module_imports,
    module_lookup,
    module_statements,
    parse_python,
    parse_source,
    python_source,
    python_trees,
    qualified_name,
    scoped_nodes,
    skill_dependency_closure,
    skill_script_directories,
    skills_by_name,
)

from deployer import platform_support, render

PLATFORM_ALLOWED = {
    "pathlib.PureWindowsPath": "reads a command's program after either path separator on every platform"
}
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
# The commands that run the program a later word names, each with its options that take the next word as their value:
# the program is the first word after the options, after the assignments env takes, and after the duration timeout
# takes. command -v and -V only look a program up.
GH_LIMIT_ALLOWED = {
    "COMMAND_WRAPPERS": "xargs's own -L, the count of lines each command it starts takes, which no gh call passes",
}
COMMAND_WRAPPERS: dict[str, frozenset[str]] = {
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "xargs": frozenset({"-I", "-n", "-L", "-d", "-P", "-s", "-E", "-a", "--arg-file", "--delimiter", "--max-args"}),
}
LOOKUP_ONLY_OPTIONS = frozenset({"-v", "-V"})
# The commands that run a script a word holds: eval its arguments, and a shell the script after its -c.
SCRIPT_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# find's actions that run the program the next word names.
FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# What a reader records for a program a wrapper runs but that no literal names, such as `xargs "$tool"`, so the command
# policy refuses it rather than passing it unseen; it never names a program.
UNREAD_PROGRAM = "<unread> "


def split_operators(token: str) -> list[str]:
    """Split a run of shell punctuation, which shlex returns as one token, into its operators."""
    if not token or any(char not in ";&|()<>\n" for char in token):
        return [token]
    parts: list[str] = []
    while token:
        operator = next(op for op in SHELL_OPERATORS if token.startswith(op))
        parts.append(operator)
        token = token[len(operator) :]
    return parts


def _substitutions(script: str) -> list[str]:
    """The script of each command substitution a Bash script holds, `$(...)` or backticks, quoted or not, outside
    single quotes, comments, and arithmetic `$((...))`."""
    found: list[str] = []
    index, quoted = 0, False
    while index < len(script):
        character = script[index]
        if character == "\\":
            index += 2
            continue
        if character == "'" and not quoted:
            index = script.find("'", index + 1) + 1 or len(script)
            continue
        if character == "#" and not quoted and (index == 0 or script[index - 1] in " \t\n;&|("):
            index = script.find("\n", index) + 1 or len(script)
            continue
        if character == '"':
            quoted = not quoted
        if script.startswith("$(", index) and not script.startswith("$((", index):
            depth, end = 1, index + 2
            while end < len(script) and depth:
                depth += {"(": 1, ")": -1}.get(script[end], 0)
                end += 1
            found.append(script[index + 2 : end - 1])
            index = end
            continue
        if character == "`":
            end = script.find("`", index + 1)
            end = len(script) if end < 0 else end
            found.append(script[index + 1 : end])
            index = end + 1
            continue
        index += 1
    return found


def _program_word(word: str) -> set[str]:
    """What a word a wrapper runs as a program names: a literal command, nothing for a template token or a path, which
    a command word is read as too, and UNREAD_PROGRAM for one built from a variable or a substitution."""
    if COMMAND_NAME.fullmatch(word):
        return {word}
    if "$" in word or "`" in word:
        return {f"{UNREAD_PROGRAM}{word}"}
    return set()


def _wrapped_programs(words: list[str]) -> set[str]:
    """The programs a simple command runs through its first word: what a wrapper such as xargs or env runs, what find's
    -exec runs, and what eval or a shell's -c script runs, followed through a wrapper of a wrapper."""
    found: set[str] = set()
    while words:
        head, rest = words[0], words[1:]
        if head == "eval":
            return found | shell_commands(" ".join(rest))
        if head in SCRIPT_SHELLS:
            script = rest[rest.index("-c") + 1 : rest.index("-c") + 2] if "-c" in rest else []
            return found | {name for text in script for name in shell_commands(text)}
        if head == "find":
            actions = [index for index, word in enumerate(rest) if word in FIND_ACTIONS and index + 1 < len(rest)]
            for index in actions:
                found |= _program_word(rest[index + 1]) | _wrapped_programs(rest[index + 1 :])
            return found
        options = COMMAND_WRAPPERS.get(head)
        if options is None or (head == "command" and LOOKUP_ONLY_OPTIONS & set(rest)):
            return found
        index = 0
        while index < len(rest) and (rest[index].startswith("-") or (head == "env" and ASSIGNMENT.match(rest[index]))):
            index += 2 if rest[index] in options else 1
        index += 1 if head == "timeout" else 0
        if index >= len(rest):
            return found
        found |= _program_word(rest[index])
        words = rest[index:]
    return found


def shell_commands(script: str, commands: list[list[str]] | None = None) -> set[str]:
    """Literal command names a Bash script runs, ignoring keywords, case patterns, and its own functions: each command
    word, each program a wrapper, find's -exec, eval, or a shell's -c script runs, and every command of a command
    substitution, quoted or not. A program a wrapper runs that no literal names is recorded as UNREAD_PROGRAM. Given
    `commands`, it also appends the words of each simple command it reads there, a substitution's included."""
    lexer = shlex.shlex(script.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    tokens = [part for token in lexer for part in split_operators(token)]
    functions = {tokens[i] for i in range(len(tokens) - 1) if tokens[i + 1] == "()"}
    functions |= {tokens[i + 1] for i in range(len(tokens) - 1) if tokens[i] == "function"}
    found: set[str] = {name for inner in _substitutions(script) for name in shell_commands(inner, commands)}
    state = "command"
    words: list[str] = []
    for word in [*tokens, "\n"]:
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
            words = [word] if word not in HEADER_KEYWORDS and word not in functions else []
            if words and COMMAND_NAME.fullmatch(word):
                found.add(word)
        elif word in COMMAND_SEPARATORS:
            found |= _wrapped_programs(words)
            if commands is not None and words:
                commands.append(words)
            words, state = [], "command"
        else:
            words.append(word)
    return found - functions


def shell_git_subcommands(script: str) -> set[str]:
    """The subcommand of each git command a Bash script runs by name, a command substitution's included, read past
    git's own options as ejryan89-skill-core's git client reads them."""
    commands: list[list[str]] = []
    shell_commands(script, commands)
    return {
        subcommand
        for words in commands
        if words[0] == "git" and (subcommand := git_client.subcommand(words[1:])) is not None
    }


# The contexts deployer/render.py gives a fence of commands for a command shell: a POSIX shell's, fish's, and cmd's.
COMMAND_SHELL_CONTEXTS = frozenset(render.FENCE_CONTEXT[label] for label in SHELL_FENCES)


def _fences(markdown: str, *contexts: str) -> list[str]:
    """The body of each fence that deployer/render.py renders in one of these contexts: shell for a Bash, sh, zsh, or
    console fence, and powershell for a PowerShell, ps1, or pwsh fence. A fence left open runs to the end of what holds
    it, as the renderer reads it, so its commands are held like any other's; unclosed_fence_problems reports it."""
    lines = markdown.splitlines()
    return [
        "\n".join(fence.text(lines[index]) for index in fence.body())
        for fence in find_fences(lines)
        if render.fence_context(fence) in contexts
    ]


# The calls that start a command, as call_tables' PROCESS_STARTS names them, and the position and keyword of the
# argument that names it. An event loop's runner methods are named under EVENT_LOOP and count on any receiver.
COMMAND_RUNNERS: dict[str, tuple[int, str]] = {name: start.command for name, start in PROCESS_STARTS.items()}
# The name each runner has in its module, which a runner taken from a module looked up by a computed name may have.
RUNNER_ATTRIBUTES = frozenset(runner.rpartition(".")[2] for runner in COMMAND_RUNNERS)
# The runners that run their command through a shell, whose every program the Bash reader reads; a runner that hands its
# keywords to subprocess.Popen does too when given shell= anything but False.
SHELL_RUNNERS = frozenset(name for name, start in PROCESS_STARTS.items() if start.through_shell)
# The runners that hand their keywords to subprocess.Popen, whose executable= replaces the program the command names.
EXECUTABLE_RUNNERS = frozenset(name for name, start in PROCESS_STARTS.items() if start.popen_keywords)
# ejryan89-skill-core's clients: each runs its one command for every call a script makes through it.
CLIENT_CLASSES = {"git_client.GitClient": "git", "github_client.GitHubClient": "gh"}
# shutil.which finds the program a script then runs. A script that takes it as an injected service calls it by its
# own name, such as services.which("dotnet"), so a call of any function named which counts too, and so do
# ejryan89-skill-core's locator's find_executable, which the deployer's platform_support also names, find_executables,
# which lists every match in its search order, and path_programs, which lists every match on PATH. A lookup takes the
# program's name first, or by one of LOOKUP_KEYWORDS.
COMMAND_LOOKUP = "shutil.which"
LOOKUP_NAMES = frozenset({"which", "path_programs", "find_executable", "find_executables"})
LOOKUP_KEYWORDS = frozenset({"cmd", "name"})
# Calls whose result names the program their first argument does: a path or text conversion, a copy into a list or a
# tuple, a split or a join of a command, and ejryan89-skill-core's located_command, which names a command's program by
# the path the locator finds for it; and the path methods that do the same for the path they are called on.
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
    return call_argument(call, 0, *sorted(LOOKUP_KEYWORDS))


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


def _text_constant(node: ast.Constant) -> ast.Constant:
    """A string or bytes literal as the text it holds, so a bytes command names its program as a string one does."""
    value = node.value.decode("utf-8", "replace") if isinstance(node.value, bytes) else node.value
    return ast.copy_location(ast.Constant(value), node)


def _read_literals(sources: list[Source]) -> list[Source]:
    return [_read_literal(source) if isinstance(source, ast.Constant) else source for source in sources]


def _reached_name(node: ast.expr, resolver: CallableResolver) -> str:
    """The dotted name an expression stands for through the module's imports, the names it binds to one, and a module
    it reaches by its name, as module_lookup reads it: subprocess.run for importlib.import_module("subprocess").run,
    and ?.run, COMPUTED_NAME standing for the module, for importlib.import_module(name).run; "" for none."""
    if isinstance(node, ast.Attribute):
        base = _reached_name(node.value, resolver)
        return f"{base}.{node.attr}" if base else ""
    return module_lookup(node, resolver) or (resolver.name(node) if isinstance(node, ast.Name) else "")


def _type_uses(tree: ast.Module, resolver: CallableResolver) -> set[int]:
    """The nodes that use a class as a type, which starts nothing: an annotation, a subscripted generic such as
    subprocess.Popen[bytes], and the class isinstance or issubclass checks against. A subscripted generic is a name
    or an attribute; a collection indexed where it is written, as {"run": subprocess.run}["run"], hands on each runner
    it holds."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name | ast.Attribute):
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
    """The scope holding each node, by its id, as scoped_nodes names it."""
    return {id(node): scope for node, scope in scoped_nodes(tree.body)}


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
        self.clients = self._client_classes()
        self._texts: dict[int, ast.Constant] = {}
        self._commands: dict[frozenset[str], set[str]] = {}
        self._problems: dict[str, list[str]] = {}
        self._unreadable: list[Unreadable] | None = None

    def _client_classes(self) -> dict[str, str]:
        """The command each ejryan89-skill-core client runs, by the client's qualified name, and the module's own
        classes that derive from one, each by its local name, so a call through a subclass runs the client's command."""
        clients = dict(CLIENT_CLASSES)
        classes = [node for node in ast.walk(self.tree) if isinstance(node, ast.ClassDef)]
        while True:
            derived = {
                f"<local>.{node.name}": command
                for node in classes
                if f"<local>.{node.name}" not in clients
                for base in node.bases
                if (command := clients.get(self.resolver.name(base)))
            }
            if not derived:
                return clients
            clients.update(derived)

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
            return [self._as_text(node)] if isinstance(node.value, (str, bytes)) else []
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

    def _as_text(self, node: ast.Constant) -> ast.Constant:
        """A string literal itself, or a bytes literal as the string it holds, placed in the literal's scope."""
        if isinstance(node.value, str):
            return node
        if id(node) not in self._texts:
            self._texts[id(node)] = _text_constant(node)
            self.scope_names[id(self._texts[id(node)])] = self.scope_names.get(id(node), MODULE_SCOPE)
        return self._texts[id(node)]

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

    def _runner(self, function: ast.expr) -> str:
        """The COMMAND_RUNNERS entry a callable expression names, or "" for none: through the module's imports, the
        names it binds to one, and a module it reaches by its name, as importlib.import_module("subprocess").run names
        subprocess.run, or an event loop's runner method called on any receiver."""
        original = self.resolver.original(function)
        for name in (self.resolver.name(function), _reached_name(original, self.resolver)):
            if name in COMMAND_RUNNERS:
                return name
        method = f"{EVENT_LOOP}.{original.attr}" if isinstance(original, ast.Attribute) else ""
        return method if method in COMMAND_RUNNERS else ""

    def _command_arguments(self, call: ast.Call) -> list[ast.expr | Unreadable]:
        """The argument of a COMMAND_RUNNERS call that names the program it starts, its executable= when it gives one,
        since that replaces the program and leaves the command's first item only the name the program is told, and
        nothing for another call."""
        name = self._runner(call.func)
        runner = COMMAND_RUNNERS.get(name)
        if runner is None:
            return []
        executable = next((item.value for item in call.keywords if item.arg == "executable"), None)
        if executable is not None and name in EXECUTABLE_RUNNERS:
            return [executable]
        argument = call_argument(call, *runner)
        if isinstance(argument, ast.Starred):
            return [Unreadable(call, f"the program in `{ast.unparse(call)}`, which unpacks its arguments")]
        return [] if argument is None else [argument]

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
        if self._runner(node):
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
        cannot follow: one handed to functools.partial, map, a Thread's target, or a collection, or taken by getattr,
        and each runner's name taken from a module the module looks up by a computed name, which may be the runner's."""
        found: list[Unreadable] = []
        types = _type_uses(self.tree, self.resolver)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and self.resolver.name(node.func) == "getattr" and len(node.args) > 1:
                module = _reached_name(node.args[0], self.resolver)
                attribute = node.args[1]
                named = (
                    isinstance(attribute, ast.Constant)
                    and isinstance(attribute.value, str)
                    and (
                        f"{module}.{attribute.value}" in COMMAND_RUNNERS
                        or (module == COMPUTED_NAME and attribute.value in RUNNER_ATTRIBUTES)
                    )
                )
                dynamic = not isinstance(attribute, ast.Constant) and any(
                    module == COMPUTED_NAME or runner.startswith(f"{module}.") for runner in COMMAND_RUNNERS
                )
                if module and (named or dynamic):
                    found.append(Unreadable(node, f"the program `{ast.unparse(node)}` runs, which getattr takes"))
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in RUNNER_ATTRIBUTES
                and _reached_name(node.value, self.resolver) == COMPUTED_NAME
            ):
                found.append(
                    Unreadable(
                        node, f"the program `{ast.unparse(node)}` runs, from a module looked up by a computed name"
                    )
                )
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

    def _runs_a_shell(self, call: ast.Call) -> bool:
        """Whether a runner call runs its command through a shell: one of SHELL_RUNNERS, or shell= anything but
        False given to a runner that hands it to subprocess.Popen."""
        name = self._runner(call.func)
        shell = next((item.value for item in call.keywords if item.arg == "shell"), None)
        given = shell is not None and not (isinstance(shell, ast.Constant) and not shell.value)
        return name in SHELL_RUNNERS or (name in EXECUTABLE_RUNNERS and given)

    def _shell_scripts(self, call: ast.Call) -> list[tuple[int, str]]:
        """The line and program of each command a script runs that a runner's list starts a shell with -c to run."""
        found: list[tuple[int, str]] = []
        for argument in self._command_arguments(call):
            items = argument.elts[:3] if isinstance(argument, (ast.List, ast.Tuple)) else []
            texts = [_text_constant(item).value if isinstance(item, ast.Constant) else None for item in items]
            if len(texts) == 3 and isinstance(texts[0], str) and isinstance(texts[2], str):
                shell = platform_support.program_name(PureWindowsPath(texts[0]).name)
                if shell in SCRIPT_SHELLS and texts[1] == "-c":
                    found += [(items[2].lineno, name) for name in sorted(shell_commands(texts[2]))]
        return found

    def started(self, call: ast.Call) -> list[tuple[int, str]]:
        """The line and program of each command a COMMAND_RUNNERS call may start, and nothing for another call: every
        program a command it runs through a shell names, as the Bash reader reads it, and every program the script a
        list starts a shell with -c to run names."""
        sources = self._sources(call)
        if self._runs_a_shell(call):
            named = [
                (source.lineno, name)
                for source in sources
                if isinstance(source, ast.Constant) and isinstance(source.value, str)
                for name in sorted(shell_commands(source.value))
            ]
        else:
            named = self._named(sources)
        return named + self._shell_scripts(call)

    def starts_process(self) -> bool:
        """Whether the module calls a command runner or an ejryan89-skill-core client, even with a dynamic command.

        An import, command lookup, or command list alone starts nothing. This does not say whether the process
        runs code from the target repository; that is the script's RUNS_REPOSITORY_CODE declaration.
        """
        return bool(self._runners_handed_on()) or any(
            self._runner(node.func) or self.resolver.name(node.func) in self.clients for node in self.calls
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
        """Every program the module runs through a runner or an ejryan89-skill-core client, or looks up with which, and
        every known tool a list or tuple begins with, since a module may hand that command to a function of its own."""
        key = frozenset(known)
        if key not in self._commands:
            found: set[str] = set()
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                    first = node.elts[0]
                    text = _text_constant(first).value if isinstance(first, ast.Constant) else None
                    if isinstance(text, str) and text in known:
                        found.add(text)
                elif isinstance(node, ast.Call):
                    client = self.clients.get(self.resolver.name(node.func))
                    found |= {client} if client else set()
                    if _is_lookup(node, self.resolver.aliases(node.func)):
                        looked_up = _read_literals(self.program_literals(_lookup_argument(node)))
                        found |= {name for _, name in self._named(looked_up)}
                    found |= {name for _, name in self.started(node)}
            self._commands[key] = found
        return self._commands[key]


@functools.cache
def _read_commands(source: str) -> CommandReader:
    return CommandReader(parse_source(source, "<commands>"))


def command_reader(path: Path) -> CommandReader:
    """The CommandReader of a Python file as it is now, built once for each content in a validation run, so every
    policy that reads a file's commands shares one reading of it and a file that changes is read again. A file no
    policy can read raises UnreadablePython."""
    return _read_commands(python_source(path))


# Prints one JSON array of {source, name} for each external command run by the PowerShell sources in the JSON file
# COMMAND_SOURCES names, as PowerShell's own parser reads them, and {source, unread} for each program it cannot read. A
# function a source defines is its own, and so is what PowerShell ships: its aliases and the commands of its core and
# of the modules under its home, such as Get-ChildItem and ForEach-Object. A module installed elsewhere, such as
# PSScriptAnalyzer, is not standard. What Start-Process starts, by -FilePath or its first positional argument, what
# `& (Get-Command name)` runs, and every command of a string Invoke-Expression runs are read as commands too; a program
# any of these, or `&` or `.`, names other than by a literal is unread, but for a .ps1 script a path names, whose own
# commands the policy reads in its file.
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
    "function Get-Literal($element) { "
    "if ($element -is [Management.Automation.Language.StringConstantExpressionAst]) { return $element.Value }; "
    "if ($element -is [Management.Automation.Language.ExpandableStringExpressionAst] -and "
    "$element.NestedExpressions.Count -eq 0) { return $element.Value }; "
    "return $null }; "
    "function Get-Argument($elements, $named, $switches) { "
    "for ($at = 1; $at -lt $elements.Count; $at++) { $element = $elements[$at]; "
    "if ($element -is [Management.Automation.Language.CommandParameterAst]) { "
    "if ($named -contains $element.ParameterName) { "
    "if ($element.Argument) { return $element.Argument }; return $elements[$at + 1] }; "
    "if (-not ($switches -contains $element.ParameterName) -and -not $element.Argument) { $at++ }; continue }; "
    "return $element }; "
    "return $null }; "
    "$found = [Collections.Generic.List[object]]::new(); "
    "$queue = [Collections.Generic.Queue[object]]::new(); "
    "for ($index = 0; $index -lt $sources.Count; $index++) { $queue.Enqueue(@($index, $sources[$index])) }; "
    "while ($queue.Count) { $item = $queue.Dequeue(); $index = $item[0]; "
    "$tokens = $null; $errors = $null; "
    "$tree = [Management.Automation.Language.Parser]::ParseInput($item[1], [ref]$tokens, [ref]$errors); "
    "$functions = @($tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name); "
    "foreach ($command in $tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.CommandAst] }, $true)) { "
    "$name = $command.GetCommandName(); $elements = $command.CommandElements; "
    "if (-not $name -and $elements[0].Extent.Text -match '\\.ps1\\W*$') { continue }; "
    "if (-not $name) { $first = $elements[0]; "
    "$inner = @($first.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)); "
    "if ($inner.Count -eq 1 -and @('Get-Command', 'gcm') -contains $inner[0].GetCommandName()) { "
    "$name = Get-Literal (Get-Argument $inner[0].CommandElements @('Name') @()) }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $first.Extent.Text }); continue } }; "
    "if (@('Start-Process', 'saps', 'start') -contains $name) { "
    "$program = Get-Argument $elements @('FilePath', 'PSPath', 'Path', 'FilePa', 'File') "
    "@('Wait', 'NoNewWindow', 'PassThru', 'UseNewEnvironment', 'LoadUserProfile'); "
    "$name = if ($program) { Get-Literal $program } else { $null }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $command.Extent.Text }); continue } }; "
    "if (@('Invoke-Expression', 'iex') -contains $name) { "
    "$script = Get-Argument $elements @('Command') @(); $text = if ($script) { Get-Literal $script } else { $null }; "
    "if ($null -eq $text) { $found.Add([ordered]@{ source = $index; unread = $command.Extent.Text }) } "
    "else { $queue.Enqueue(@($index, $text)) }; continue }; "
    "if ($functions -contains $name -or $own.Contains($name)) { continue }; "
    "$found.Add([ordered]@{ source = $index; name = $name }) } }; "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $found.ToArray() -Compress -Depth 3))"
)


def powershell_commands(sources: list[str]) -> list[set[str]]:
    """The external programs each PowerShell source runs, read by PowerShell's own parser in one call for them all."""
    if not sources:
        return []
    found = powershell_json(
        POWERSHELL_COMMANDS_RUN, sources, "COMMAND_SOURCES", "PowerShell could not read the commands"
    )
    commands: list[set[str]] = [set() for _ in sources]
    for item in found:
        if "unread" in item:
            commands[item["source"]].add(f"{UNREAD_PROGRAM}{item['unread']}")
        elif name := _program_name(item["name"]):
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
    """The programs a file runs, where powershell holds what each PowerShell source read for the run runs. A Python
    file no policy can read runs nothing here; skill_command_problems reports it."""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    found = {command for source in _powershell_sources(path) for command in powershell[source]}
    if suffix == ".py":
        with contextlib.suppress(UnreadablePython):
            found |= command_reader(path).commands(known)
    elif suffix in {".sh", ".bash"}:
        found |= shell_commands(text)
    elif suffix == ".md":
        found |= {command for block in _fences(text, *COMMAND_SHELL_CONTEXTS) for command in shell_commands(block)}
    return found


def _skill_files(directory: Path) -> list[Path]:
    """A skill's files, without its tests."""
    return sorted(path for path in directory.rglob("*") if path.is_file() and not is_test_script(path))


NAMED_SCRIPT = re.compile(r"/([a-z0-9-]+)/scripts/([A-Za-z_]\w*)\.py\b")


def _referenced_scripts(path: Path) -> frozenset[str]:
    """Module names a file imports, and script names it gives by a path through another skill's scripts/; none for a
    Python file no policy can read, which skill_command_problems reports."""
    if path.suffix.casefold() != ".py":
        return _references(path.read_text(encoding="utf-8", errors="replace"), False)
    try:
        return _references(python_source(path), True)
    except UnreadablePython:
        return frozenset()


@functools.cache
def _references(text: str, python: bool) -> frozenset[str]:
    """What _referenced_scripts finds in a file's text, read once for each content in a validation run."""
    if not python:
        return frozenset(name for _, name in NAMED_SCRIPT.findall(text))
    try:
        tree = parse_source(text, "<references>")
    except UnreadablePython:
        return frozenset()
    # A module-level sanction such as DUPLICATION_ALLOWED names, in its reasons, the other files that hold a copy,
    # which the module does not run.
    sanctions = {
        line
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id.endswith("_ALLOWED")
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    unsanctioned = "".join(
        line for number, line in enumerate(text.splitlines(keepends=True), 1) if number not in sanctions
    )
    return frozenset({name for _, name in NAMED_SCRIPT.findall(unsanctioned)}) | {
        found.module.split(".")[0] for found in module_imports(tree)
    }


def _reached_dependency_scripts(root: Path, skill: str, own: list[Path]) -> list[Path]:
    """The dependency scripts a skill runs: those its own files import or name, and what those import in turn."""
    scripts: dict[str, Path] = {}
    skills = skills_by_name(root)
    for dependency in skill_dependency_closure(root, skill):
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
    skill declares them too, so a skill that reaches ejryan89-skill-core's GitHub client declares gh.
    """
    skills: list[tuple[str, set[str], list[Path], list[Path]]] = []
    directories = skills_by_name(root)
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
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
                name = path.relative_to(root).as_posix()
                try:
                    problems += command_reader(path).problems(name)
                except UnreadablePython as error:
                    problems.append(f"{name} {error}")
        used = {command for path in own for command in _file_commands(path, known, powershell)}
        reached: dict[str, Path] = {}
        for path in dependency_scripts:
            for command in _file_commands(path, known, powershell):
                reached.setdefault(command, path)
        unread = sorted(name for name in [*used, *reached] if name.startswith(UNREAD_PROGRAM))
        problems += [
            f"skill {skill} runs a program it names by `{name.removeprefix(UNREAD_PROGRAM)}`, which the policy cannot "
            f"read; name the program with a literal; see {COMMANDS_DOC}"
            for name in dict.fromkeys(unread)
        ]
        used -= set(unread)
        reached = {name: path for name, path in reached.items() if name not in unread}
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


def optional_tool_placement_problems(root: Path, always_optional: set[str]) -> list[str]:
    """Report a skill that lists under tools a tool the catalogue makes optional for every skill.

    check, the deploy warning, and the skill reference treat such a tool as optional whatever a skill declares, so
    the declaration says so too and the metadata and the catalogue cannot disagree.
    """
    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
        problems.extend(
            f"skill {metadata.stem} lists {name} under tools, which the catalogue makes optional for every "
            f"skill; declare it in optional_tools; see {COMMANDS_DOC}"
            for name in sorted(set(document.get("tools", [])) & always_optional)
        )
    return problems


GH_SHELL_CALL = re.compile(r"(?:^|[\s;|&(])gh\s")
# A word that passes gh a filter, in each form gh's flag parser reads: --jq or --template alone, with `=` and the
# filter, and -q alone or with the filter attached. The Python and the shell readers both read a word by it.
GH_FILTER_WORD = re.compile(r"(--jq|--template)(?:=.*)?|(-q).*", re.DOTALL)
GH_SHELL_WORD_BREAK = re.compile(r"[\s;|&()]+")


def gh_filter_flag(word: str) -> str | None:
    """The filter flag a word passes gh, as --jq, --template, or -q, or None when it passes none."""
    match = GH_FILTER_WORD.fullmatch(word)
    return None if match is None else match.group(1) or match.group(2)


def _gh_filter_flags(tree: ast.Module) -> list[tuple[int, str]]:
    """The line and flag of each gh filter a Python module passes."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            flag = gh_filter_flag(node.value)
            if flag in ("--jq", "--template"):
                found.append((node.lineno, flag))
        elif isinstance(node, (ast.List, ast.Tuple)):
            values = [
                item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            # gh's -q applies a jq filter to --json output; git's -q never comes with --json.
            json_output = any(value == "--json" or value.startswith("--json=") for value in values)
            if json_output and any(gh_filter_flag(value) == "-q" for value in values):
                found.append((node.lineno, "-q"))
    return found


def _gh_shell_filter_flags(text: str) -> list[tuple[int, str]]:
    """The line and flag of each gh filter a shell script passes on a line that runs gh."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if GH_SHELL_CALL.search(line):
            found += [
                (number, flag) for word in GH_SHELL_WORD_BREAK.split(line) if (flag := gh_filter_flag(word)) is not None
            ]
    return found


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
            for _, _, tree in python_trees([path], root, problems):
                found += _gh_filter_flags(tree)
        elif path.suffix.casefold() in {".sh", ".bash", ".ps1"}:
            found += _gh_shell_filter_flags(text)
        for number, flag in sorted(set(found)):
            problems.append(
                f"{name}:{number} filters gh output with {flag}; parse its JSON in Python instead; see {COMMANDS_DOC}"
            )
    return problems


CONSOLE_RUNNER = "run_main"
CONSOLE_FUNCTION = f"console.{CONSOLE_RUNNER}"
CONSOLE_DOC = '"Script results" in docs/adding-a-skill.md'


def console_entry_points(root: Path) -> list[Path]:
    """The Python that can run as a program: every shipped and repository skill's scripts outside ejryan89-skill-core,
    deploy.py, the deployer, and tools/, regression suites aside."""
    files = [
        path
        for scripts in skill_script_directories(root)
        if scripts.parent.name != SKILL_CORE
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


def main_blocks(tree: ast.Module) -> list[ast.If]:
    """Each `if __name__ == "__main__":` block that runs at module level, one in a module-level if, try, or with block
    included, and its test written either way round."""
    return [node for node in module_statements(tree.body) if isinstance(node, ast.If) and is_main_guard(node.test)]


# The functions an entry point runs as its program, by name.
ENTRY_FUNCTIONS = frozenset({"main", "_main"})


def _unguarded_main_lines(tree: ast.Module, aliases: dict[str, str]) -> list[int]:
    """The lines of each module-level statement outside every __main__ block that runs main, or run_main, as the
    program, itself or as what sys.exit or SystemExit is given, so it runs whenever the module is imported."""
    guarded = {id(node) for block in main_blocks(tree) for node in ast.walk(block)}
    lines: list[int] = []
    for statement in module_statements(tree.body):
        if id(statement) in guarded:
            continue
        call = statement.value if isinstance(statement, ast.Expr) else getattr(statement, "exc", None)
        if (
            isinstance(call, ast.Call)
            and qualified_name(call.func, aliases) in {"sys.exit", "SystemExit"}
            and call.args
        ):
            call = call.args[0]
        if isinstance(call, ast.Call) and (
            (isinstance(call.func, ast.Name) and call.func.id in ENTRY_FUNCTIONS)
            or qualified_name(call.func, aliases) == CONSOLE_FUNCTION
        ):
            lines.append(statement.lineno)
    return lines


def _runs_through_the_runner(statement: ast.stmt, aliases: dict[str, str]) -> bool:
    """Whether a statement calls ejryan89-skill-core's run_main, resolved through the module's imports."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and qualified_name(statement.value.func, aliases) == CONSOLE_FUNCTION
    )


def console_setup_problems(root: Path) -> list[str]:
    """Report an entry point whose __main__ block does not start by calling ejryan89-skill-core's run_main(), and a
    module that reconfigures a stream's encoding itself.

    Skill scripts, deploy.py, the deployer, and tools/ print paths and text the user wrote, which a Windows pipe's
    legacy code page cannot encode, and promise a FAILED line, never a traceback, when they fail; one runner sets up
    the console and keeps that promise, so the copies cannot drift apart again.
    """
    problems: list[str] = []
    for _, name, tree in python_trees(console_entry_points(root), root, problems):
        aliases = import_aliases(tree)
        problems += [
            f"{name}:{line} reconfigures a stream itself; call {CONSOLE_RUNNER}() from {SKILL_CORE} instead; "
            f"see {CONSOLE_DOC}"
            for line in _stream_change_lines(tree, aliases)
        ]
        for node in main_blocks(tree):
            if not _runs_through_the_runner(node.body[0], aliases):
                problems.append(
                    f"{name}:{node.lineno} does not call {SKILL_CORE}'s {CONSOLE_RUNNER}() first in its __main__ "
                    f"block; see {CONSOLE_DOC}"
                )
        problems += [
            f'{name}:{line} runs main outside an `if __name__ == "__main__":` block, so importing it runs it; run '
            f"{SKILL_CORE}'s {CONSOLE_RUNNER}(main) first in that block instead; see {CONSOLE_DOC}"
            for line in _unguarded_main_lines(tree, aliases)
        ]
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
    """Whether a message holds the word FAILED in any literal it is built from: a string, an f-string, a
    concatenation, or a % or .format() template."""
    return any(
        isinstance(part, ast.Constant) and isinstance(part.value, str) and "FAILED" in part.value
        for part in ast.walk(node)
    )


# The standard streams, by every name sys gives them, and what ends the process with a code: sys.exit, the exit and
# quit builtins, os._exit, and raising SystemExit.
STANDARD_ERROR = frozenset({"sys.stderr", "sys.__stderr__"})
STANDARD_OUTPUT = frozenset({"sys.stdout", "sys.__stdout__"})
EXIT_CALLS = frozenset({"sys.exit", "exit", "quit", "builtins.exit", "builtins.quit", "os._exit", "SystemExit"})
PARSER_CLASS = "argparse.ArgumentParser"


def _parser_names(tree: ast.Module, resolver: CallableResolver) -> set[str]:
    """The names the module binds to an argparse.ArgumentParser it builds, in any scope."""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Call)
        and resolver.name(node.value.func) == PARSER_CLASS
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }


def _is_parser(receiver: ast.expr, parsers: set[str]) -> bool:
    """Whether a receiver is an argument parser: a name or attribute named for one, or a name bound to one."""
    name = (
        receiver.id if isinstance(receiver, ast.Name) else receiver.attr if isinstance(receiver, ast.Attribute) else ""
    )
    return "parser" in name.casefold() or name in parsers


def _call_breaches(node: ast.Call, resolver: CallableResolver, parsers: set[str], in_handler: bool) -> list[str]:
    """What one call does that "Script results" rules out."""
    breaches: list[str] = []
    function = resolver.name(node.func)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "error"
        and in_handler
        and _is_parser(node.func.value, parsers)
    ):
        breaches.append("reports a failure through parser.error; print FAILED <reason> and exit 1")
    code = call_argument(node, 0, "status" if function == "os._exit" else "code") if function in EXIT_CALLS else None
    if isinstance(code, ast.Constant) and type(code.value) is int and code.value not in CONTRACT_EXIT_CODES:
        breaches.append(f"exits {code.value}; scripts exit only 0, 1, or 2")
    stream: ast.expr | None = None
    message = node.args[0] if node.args else None
    if function == "print":
        stream = next((keyword.value for keyword in node.keywords if keyword.arg == "file"), None)
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "write":
        stream = node.func.value
    elif function == "json.dump":
        stream, message = call_argument(node, 1, "fp"), node
    target = resolver.name(stream) if stream is not None else "sys.stdout" if function == "print" else ""
    if target in STANDARD_ERROR and message is not None and _mentions_failed(message):
        breaches.append("prints FAILED on stderr; print it on stdout")
    if (
        target in STANDARD_OUTPUT
        and isinstance(message, ast.Call)
        and resolver.name(message.func) in {"json.dumps", "json.dump"}
    ):
        breaches.append("prints JSON; print one fact per line")
    return breaches


def _contract_breaches(tree: ast.Module) -> list[tuple[int, str]]:
    """Where a script reports in a way "Script results" rules out, with what it does instead, each name read through
    the module's imports and the names it binds to one."""
    breaches: list[tuple[int, str]] = []
    resolver = CallableResolver(tree)
    parsers = _parser_names(tree, resolver)
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    in_handler = {id(inner) for handler in handlers for statement in handler.body for inner in ast.walk(statement)}
    entry_points = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in {"main", "_main"}
    ]
    for entry_point in entry_points:
        breaches.extend(
            (node.lineno, f"exits {node.value.value}; scripts exit only 0, 1, or 2")
            for node in ast.walk(entry_point)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
            and node.value.value not in CONTRACT_EXIT_CODES
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            breaches += [
                (node.lineno, breach) for breach in _call_breaches(node, resolver, parsers, id(node) in in_handler)
            ]
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
    ordered = sorted(
        (path for path in files if path.is_file() and not is_test_script(path)), key=lambda p: p.as_posix()
    )
    for _, name, tree in python_trees(ordered, root, problems):
        problems.extend(
            f"{name}:{node.lineno}: function {node.name} is named so CodeQL treats its result as a secret "
            "and flags printing it; name it for what it returns"
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and CODEQL_SECRET_NAME.search(node.name)
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
            try:
                tree = parse_python(path)
            except UnreadablePython as error:
                problems.append(f"{name} {error}")
                continue
            exemption = _contract_exemption(tree)
            if exemption is None:
                problems.append(f"{name}: {CONTRACT_EXEMPTION} must be a non-empty string saying why")
            elif not exemption:
                problems += [f"{name}:{line} {breach}; see {RESULTS_DOC}" for line, breach in _contract_breaches(tree)]
        elif path.suffix in {".sh", ".bash"}:
            for number, line in enumerate(text.splitlines(), 1):
                for code in SHELL_EXIT.findall(line.split("#", 1)[0]):
                    # A code past the digit limit, or in digits other than ASCII, is outside the contract too.
                    if whole_number.parse(code) not in CONTRACT_EXIT_CODES:
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
        if isinstance(first, ast.Constant) and isinstance(first.value, (str, bytes)):
            found = [(first.lineno, str(_text_constant(first).value).split(" ", 1)[0])]
    elif isinstance(node, ast.Call):
        found = reader.started(node)
    return [(line, command) for line, command in found if command in CLIENT_COMMANDS]


def client_command_problems(root: Path) -> list[str]:
    """Report a Python skill script outside ejryan89-skill-core that runs git or gh itself instead of through
    ejryan89-skill-core.

    The clients read no stdin, turn every prompt off, bound each command with a timeout, and classify its failures;
    a command a script starts itself has none of that, so a sweep can wait forever on a credential prompt.
    """
    problems: list[str] = []
    for path in sorted(path for scripts in skill_script_directories(root) for path in scripts.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        if is_test_script(path) or posix_within(name, SKILL_CORE_SCRIPTS, strict=True):
            continue
        try:
            reader = command_reader(path)
        except UnreadablePython as error:
            problems.append(f"{name} {error}")
            continue
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

    def test_skills_declare_a_tool_optional_for_every_skill_as_optional(self) -> None:
        from deployer import tools

        always_optional = {name for name, tool in tools.SKILL_TOOLS.items() if tool.optional}
        self.assertEqual([], optional_tool_placement_problems(REPOSITORY_ROOT, always_optional))

    def test_skill_scripts_parse_gh_json_instead_of_filtering_it(self) -> None:
        self.assertEqual([], gh_filter_problems(REPOSITORY_ROOT))

    def test_skill_scripts_run_git_and_gh_through_skill_core(self) -> None:
        self.assertEqual([], client_command_problems(REPOSITORY_ROOT))

    def test_entry_points_set_up_the_console_through_skill_core(self) -> None:
        self.assertEqual([], console_setup_problems(REPOSITORY_ROOT))

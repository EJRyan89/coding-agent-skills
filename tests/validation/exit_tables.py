"""Each entry point's exit table: the codes its module docstring says it exits with, held to the codes its code can
exit with.

An entry point is a Python module whose `if __name__ == "__main__":` block calls ejryan89-skill-core's run_main,
resolved through its imports, found by reading every Python file of the repository outside its suites and fixtures:
nothing registers one by hand, so a new entry point is held from the pull request that adds it. Its module docstring
holds one table, in the form "Script results" in docs/adding-a-skill.md settles:

    Exit status:
      0  what exit 0 means
      1  what exit 1 means, a row's text continuing on lines
         indented further

A row's code is a whole number; the rows run in ascending order, each code once. The table names every code the
module can exit with, and no other: 1, which run_main exits with on a failure no handler caught; 2 when the code it
reaches parses arguments with argparse, whose usage error exits 2; each code an exit call or a raised SystemExit
is given, in any function the entry point reaches, where none or None exits 0, a string exits 1, and any other
argument is read as a return is; and each code the function run_main runs returns, read through what it returns: a
number, a module constant bound to one, either branch of a conditional, a local name bound to one of those, and the
result of a function it calls, a function of its own module or one an import names, by name, through a parameter
given a function, or through a table of functions it indexes or looks up. A function that ends without a return
returns None, which exits 0. Ctrl+C's 130 is run_main's alike for every entry point, stated once in "Script
results", so a table names it only where the code gives it a meaning of its own.

A return or an exit call whose code the reader cannot follow, such as an attribute of an object, is named with where
its codes come from in a module-level `EXIT_CODES_ALLOWED = {"<expression as written>": "<reason>"}` in the module
that holds it or in the entry point, each reason a non-empty string; an entry point that reaches one may name codes
the reader does not see, and its table is held only to name each code the reader does see. An entry that sanctions
no return fails, read in every module, whether or not an entry point still reaches it. A run that finds no entry
point fails, since it read no table.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import file_presence
import whole_number
from path_containment import within
from skill_scripts import EXIT_CALLS, PARSER_CLASS, RESULTS_DOC, _is_parser, main_blocks
from validation_support import (
    REPOSITORY_ROOT,
    UnreadablePython,
    call_argument,
    import_aliases,
    is_test_script,
    module_allowance,
    parse_python,
    python_source,
    qualified_name,
    repository_files,
)

RUNNER = "console.run_main"
TABLE_HEADING = "Exit status:"
ROW = re.compile(r"^  (\d+) +(\S.*)$")
CONTINUATION = re.compile(r"^ {4,}\S")
ALLOWANCE = "EXIT_CODES_ALLOWED"
# An error's code, which a return reads as each code a reached call passes by this keyword, and each default a
# parameter of this name takes.
ERROR_CODE = "exit_code"
# What run_main exits with on a failure no handler caught, and on Ctrl+C, which a table need not name.
RUNNER_FAILURE = 1
INTERRUPTED = 130
# What argparse exits with on a usage error, and the calls that reach it.
USAGE = 2
PARSING_CALLS = frozenset({"parse_args", "parse_known_args", "parse_intermixed_args", "parse_known_intermixed_args"})
# A parser's own usage error, which exits 2 too. Loggers and other objects have an error method, so a call of it is
# read as the usage exit only on a receiver skill_scripts reads as a parser, or one the module binds to a parser.
USAGE_ERROR_CALL = "error"
SUBPARSER_CALL = "add_parser"
# The folder below the repository whose Python never runs as a program the repository ships or uses.
NOT_SCANNED = Path("tests") / "fixtures"
SKILL_CORE_SCRIPTS = Path("skills") / "ejryan89-skill-core" / "scripts"


@dataclass(frozen=True)
class EntryPoint:
    """One entry point: its path from the repository root, its file, and its syntax tree."""

    name: str
    path: Path
    tree: ast.Module


def runs_the_runner(tree: ast.Module) -> bool:
    """Whether a module's `__main__` block calls run_main, resolved through its imports."""
    aliases = import_aliases(tree)
    return any(
        isinstance(node, ast.Call) and qualified_name(node.func, aliases) == RUNNER
        for block in main_blocks(tree)
        for node in ast.walk(block)
    )


def scanned_files(root: Path, files: list[Path] | None = None) -> list[Path]:
    """The Python files the policy reads, among the repository's files or the files given: every one outside the
    suites, the root conftest.py, and tests/fixtures/, in order of their path from the root."""
    listed = repository_files(root) if files is None else files
    return [
        path
        for path in sorted(listed, key=lambda each: each.relative_to(root).as_posix())
        if path.suffix == ".py"
        and not is_test_script(path)
        and not within(path, root / NOT_SCANNED)
        and path.name != "conftest.py"
    ]


def entry_points(
    root: Path = REPOSITORY_ROOT, problems: list[str] | None = None, files: list[Path] | None = None
) -> list[EntryPoint]:
    """Every entry point among the repository's files, or the files given, by its path from the root; a file no
    policy can read goes in problems."""
    found: list[EntryPoint] = []
    for path in scanned_files(root, files):
        name = path.relative_to(root).as_posix()
        try:
            tree = parse_python(path)
        except UnreadablePython as error:
            if problems is not None:
                problems.append(f"{name} {error}")
            continue
        if runs_the_runner(tree):
            found.append(EntryPoint(name, path, tree))
    return found


class TableUnreadable(ValueError):
    """A docstring's exit table that is not in the settled form; the message says what is wrong."""


def exit_table(docstring: str | None) -> dict[int, str]:
    """The exit table a module docstring holds, each code with what it means; TableUnreadable when it holds none, more
    than one, or one out of form."""
    lines = (docstring or "").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == TABLE_HEADING]
    if not starts:
        raise TableUnreadable(f"has no `{TABLE_HEADING}` table in its module docstring")
    if len(starts) > 1:
        raise TableUnreadable(f"has {len(starts)} `{TABLE_HEADING}` tables in its module docstring, not one")
    table: dict[int, str] = {}
    last: int | None = None
    for line in lines[starts[0] + 1 :]:
        if not line.strip():
            break
        row = ROW.match(line)
        if row is not None:
            code = whole_number.read(row.group(1), TableUnreadable(f"names exit {row.group(1)}, which is no exit code"))
            if last is not None and code <= last:
                raise TableUnreadable(f"names exit {code} after exit {last}; list each code once, in ascending order")
            table[code] = row.group(2)
            last = code
        elif last is not None and CONTINUATION.match(line):
            table[last] += " " + line.strip()
        else:
            raise TableUnreadable(
                f"holds the line {line.strip()!r} in its exit table, which is no row: `  <code>  ...`"
            )
    if not table:
        raise TableUnreadable(f"has a `{TABLE_HEADING}` heading with no row below it")
    return table


@dataclass
class Module:
    """One module the reader follows into: its file, tree, top-level functions, int constants, and imports."""

    path: Path
    tree: ast.Module
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    constants: dict[str, int] = field(default_factory=dict)
    dictionaries: dict[str, ast.Dict] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, str] = field(default_factory=dict)
    # Whether its EXIT_CODES_ALLOWED is no literal dictionary of non-empty reasons, as module_allowance reads one.
    allowance_malformed: bool = False
    # Each property a class of the module defines, by its name.
    properties: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = field(default_factory=dict)
    # The names it binds to an argument parser, in any scope.
    parsers: set[str] = field(default_factory=set)

    @classmethod
    def read(cls, path: Path, tree: ast.Module, package: str = "") -> Module:
        module = cls(path, tree, aliases={**relative_aliases(tree, package), **import_aliases(tree)})
        module.parsers = parser_names(tree, module.aliases)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                module.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        isinstance(decorator, ast.Name) and decorator.id == "property"
                        for decorator in method.decorator_list
                    ):
                        module.properties.setdefault(method.name, []).append(method)
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            value = getattr(node, "value", None)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(value, ast.Constant) and type(value.value) is int:
                    module.constants[target.id] = value.value
                elif isinstance(value, ast.Dict):
                    module.dictionaries[target.id] = value
        allowed = module_allowance(tree, ALLOWANCE)
        module.allowed = allowed or {}
        module.allowance_malformed = allowed is None
        return module


def is_parser_annotation(annotation: ast.expr | None, aliases: dict[str, str]) -> bool:
    """Whether an annotation names argparse's parser, as a name or a quoted one."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    return annotation is not None and qualified_name(annotation, aliases) == PARSER_CLASS


def parser_names(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """The names a module binds to an argument parser, in any scope: one assigned argparse.ArgumentParser(...), a
    subcommand's add_parser(...), or a call of its own function annotated to return a parser, and a parameter or a
    name annotated as one."""
    builders = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_parser_annotation(node.returns, aliases)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and is_parser_annotation(node.annotation, aliases):
            found.add(node.arg)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and is_parser_annotation(node.annotation, aliases)
        ):
            found.add(node.target.id)
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if (
            qualified_name(callee, aliases) == PARSER_CLASS
            or (isinstance(callee, ast.Attribute) and callee.attr == SUBPARSER_CALL)
            or (isinstance(callee, ast.Name) and callee.id in builders)
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.update(target.id for target in targets if isinstance(target, ast.Name))
    return found


def relative_aliases(tree: ast.Module, package: str) -> dict[str, str]:
    """What each name a relative import binds stands for, as a dotted name from the repository root: in the deployer,
    `from . import pipeline` binds pipeline to deployer.pipeline."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level > 0 and package):
            continue
        parts = package.split(".")
        base = parts[: len(parts) - (node.level - 1)]
        owner = ".".join([*base, *([node.module] if node.module else [])])
        found |= {alias.asname or alias.name: f"{owner}.{alias.name}" for alias in node.names}
    return found


Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Scope:
    """Where an expression is read: its module, the function that holds it, and what that function's parameters
    were given by the call the reader followed, each with the scope it was given in."""

    module: Module
    function: Function | None
    given: tuple[tuple[str, tuple[ast.expr, Scope]], ...] = ()

    def argument(self, name: str) -> tuple[ast.expr, Scope] | None:
        return dict(self.given).get(name)


def _own_nodes(function: Function) -> Iterator[ast.AST]:
    """Every node of a function's body outside the functions, lambdas, and classes it defines."""
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _assigned(function: Function, name: str) -> list[tuple[ast.expr, int | None]]:
    """Each value a function's own body binds to a name, by assignment or annotated assignment, with the position the
    name takes when it is one of a tuple's targets, as `code` in `code, lines = run()`."""
    found: list[tuple[ast.expr, int | None]] = []
    for node in _own_nodes(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append((node.value, None))
                elif isinstance(target, ast.Tuple):
                    found += [
                        (node.value, index)
                        for index, each in enumerate(target.elts)
                        if isinstance(each, ast.Name) and each.id == name
                    ]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            found.append((node.value, None))
    return found


def _falls_through(body: list[ast.stmt]) -> bool:
    """Whether a block can end without a return or a raise, read by its last statement."""
    if not body:
        return True
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return False
    if isinstance(last, ast.If):
        return _falls_through(last.body) or _falls_through(last.orelse)
    if isinstance(last, ast.Try):
        ends = [last.body if not last.orelse else last.orelse, *(handler.body for handler in last.handlers)]
        return any(_falls_through(block) for block in ends) and _falls_through(last.finalbody)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _falls_through(last.body)
    if isinstance(last, ast.Match):
        return True
    return not (
        isinstance(last, ast.Expr)
        and isinstance(last.value, ast.Call)
        and isinstance(last.value.func, (ast.Name, ast.Attribute))
        and (last.value.func.id if isinstance(last.value.func, ast.Name) else last.value.func.attr) in {"exit", "_exit"}
    )


@dataclass
class Reading:
    """What the reader found from one entry point: the codes it can exit with, and the returns it could not follow,
    each by its file, line, and expression."""

    codes: set[int] = field(default_factory=set)
    unread: list[tuple[Path, int, str]] = field(default_factory=list)


class CodeReader:
    """Reads the codes one entry point can exit with, following into the modules its imports name."""

    def __init__(self, root: Path, entry: Path) -> None:
        self.root = root
        self.folders = [entry.parent, root / SKILL_CORE_SCRIPTS, root]
        self.modules: dict[Path, Module] = {}
        self.reading = Reading()
        self.returned: set[tuple[int, int | None, tuple[tuple[str, int], ...]]] = set()
        self.reached: set[int] = set()
        # Whether a return reads an error's exit_code, and each code a reached call passes as exit_code.
        self.reads_error_code = False
        self.error_codes: set[int] = set()
        # The modules whose reached code parses the command line, where its subcommands are declared.
        self.parsing: set[Path] = set()

    def module_at(self, path: Path, tree: ast.Module | None = None) -> Module | None:
        if path not in self.modules:
            package = (
                ".".join(path.parent.relative_to(self.root).parts)
                if file_presence.is_present(path.parent / "__init__.py", other_is_absent=True)
                and within(path, self.root)
                else ""
            )
            try:
                self.modules[path] = Module.read(path, tree if tree is not None else parse_python(path), package)
            except (UnreadablePython, OSError):
                return None
        return self.modules[path]

    def code_of(self, module: Module, node: ast.expr) -> int | None:
        """The number an expression is: a literal, or a name constant as `constant` reads it."""
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        return self.constant(module, node)

    def error_defaults(self) -> set[int]:
        """Each default a parameter named exit_code takes in a module the reader followed."""
        found: set[int] = set()
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                arguments = node.args
                positional = [*arguments.posonlyargs, *arguments.args]
                pairs = [
                    *zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True),
                    *(
                        (each, default)
                        for each, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
                    ),
                ]
                found |= {
                    number
                    for parameter, default in pairs
                    if parameter.arg == ERROR_CODE
                    and default is not None
                    and (number := self.code_of(module, default)) is not None
                }
        return found

    def constant(self, module: Module, node: ast.expr) -> int | None:
        """The number a name stands for: a constant of its own module, or one an import names."""
        if isinstance(node, ast.Name) and node.id in module.constants and node.id not in module.aliases:
            return module.constants[node.id]
        dotted = qualified_name(node, module.aliases)
        owner, _, name = dotted.rpartition(".")
        other = self.module_named(owner) if owner else None
        return other.constants.get(name) if other is not None else None

    def module_named(self, dotted: str) -> Module | None:
        relative = Path(*dotted.split("."))
        for folder in self.folders:
            for candidate in (folder / relative.with_suffix(".py"), folder / relative / "__init__.py"):
                if file_presence.is_present(candidate, other_is_absent=True):
                    return self.module_at(candidate)
        return None

    def function_named(self, module: Module, node: ast.expr) -> tuple[Module, Function] | None:
        """The function a name or attribute names, in its own module or in one an import names."""
        if isinstance(node, ast.Name) and node.id in module.functions and node.id not in module.aliases:
            return module, module.functions[node.id]
        dotted = qualified_name(node, module.aliases)
        if "." not in dotted:
            return None
        owner, _, name = dotted.rpartition(".")
        other = self.module_named(owner)
        if other is not None and name in other.functions:
            return other, other.functions[name]
        return None

    def callables(self, node: ast.expr, scope: Scope) -> list[tuple[Module, Function]] | None:
        """The functions a called expression can be, or None when the reader cannot tell."""
        named = self.function_named(scope.module, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if named is not None:
            return [named]
        if isinstance(node, ast.Name):
            given = scope.argument(node.id)
            if given is not None:
                return self.callables(*given)
            values = _assigned(scope.function, node.id) if scope.function is not None else []
            if values and all(position is None for _, position in values):
                return self._all([value for value, _ in values], scope)
            if node.id in scope.module.dictionaries:
                return self._all(list(scope.module.dictionaries[node.id].values), Scope(scope.module, None))
            return None
        if isinstance(node, ast.Subscript):
            return self.callables(node.value, scope)
        if isinstance(node, ast.Dict):
            return self._all(list(node.values), scope)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            return self.callables(node.func.value, scope)
        return None

    def _all(self, values: list[ast.expr], scope: Scope) -> list[tuple[Module, Function]] | None:
        found: list[tuple[Module, Function]] = []
        for value in values:
            each = self.callables(value, scope)
            if each is None:
                return None
            found += each
        return found

    def call_scope(self, call: ast.Call, module: Module, function: Function, scope: Scope) -> Scope:
        """The scope a followed call runs its function in, each parameter given the argument the call passes."""
        parameters = [*function.args.posonlyargs, *function.args.args]
        given = [
            (parameter.arg, (argument, scope))
            for parameter, argument in zip(parameters, call.args, strict=False)
            if not isinstance(argument, ast.Starred)
        ]
        given += [(keyword.arg, (keyword.value, scope)) for keyword in call.keywords if keyword.arg is not None]
        return Scope(module, function, tuple(given))

    def returns(self, module: Module, function: Function, scope: Scope, element: int | None = None) -> None:
        """Add the codes a function returns, or the item at `element` of the tuples it returns, to the reading."""
        key = (id(function), element, tuple(sorted((name, id(value)) for name, (value, _) in scope.given)))
        if key in self.returned:
            return
        self.returned.add(key)
        self.reach(module, function)
        for node in _own_nodes(function):
            if isinstance(node, ast.Return):
                self.value(node.value, scope, element)
        if _falls_through(function.body) and element is None:
            self.reading.codes.add(0)

    def value(self, node: ast.expr | None, scope: Scope, element: int | None = None) -> None:
        """Add the codes one returned expression can be, or the item at `element` of the tuple it is."""
        if element is not None and isinstance(node, ast.Tuple) and element < len(node.elts):
            self.value(node.elts[element], scope)
        elif element is None and (code := self.code(node, scope)) is not None:
            self.reading.codes.add(code)
        elif isinstance(node, ast.IfExp):
            self.value(node.body, scope, element)
            self.value(node.orelse, scope, element)
        elif isinstance(node, ast.Name) and (given := scope.argument(node.id)) is not None:
            self.value(given[0], given[1], element)
        elif isinstance(node, ast.Name) and scope.function is not None and _assigned(scope.function, node.id):
            self.assigned(node, scope, element)
        elif isinstance(node, ast.Call) and (functions := self.callables(node.func, scope)) is not None:
            for module, function in functions:
                self.returns(module, function, self.call_scope(node, module, function, scope), element)
        elif isinstance(node, ast.Attribute) and node.attr in scope.module.properties:
            for method in scope.module.properties[node.attr]:
                self.returns(scope.module, method, Scope(scope.module, method), element)
        elif element is None and isinstance(node, ast.Attribute) and node.attr == ERROR_CODE:
            self.reads_error_code = True
        else:
            self._unread(node, scope)

    def code(self, node: ast.expr | None, scope: Scope) -> int | None:
        """The code an expression is as it stands: None returned, which exits 0, a number, or a name bound to one."""
        if node is None or (isinstance(node, ast.Constant) and node.value is None):
            return 0
        return self.code_of(scope.module, node)

    def assigned(self, node: ast.Name, scope: Scope, element: int | None) -> None:
        """Add the codes each value the function binds to a local name can be."""
        assert scope.function is not None
        for value, position in _assigned(scope.function, node.id):
            if position is None:
                self.value(value, scope, element)
            elif element is None:
                self.value(value, scope, position)
            else:
                self._unread(node, scope)

    def _unread(self, node: ast.expr | None, scope: Scope) -> None:
        expression = ast.unparse(node) if node is not None else "None"
        self.reading.unread.append((scope.module.path, getattr(node, "lineno", 0), expression))

    def exits(self, argument: ast.expr | None, scope: Scope) -> None:
        """Add the codes an exit call or a raised SystemExit is given: no argument or None exits 0, a string is printed
        and exits 1, and any other argument is read as a returned expression is, so one the reader cannot follow is
        recorded as unread."""
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            self.reading.codes.add(1)
        else:
            self.value(argument, scope)

    def reach(self, module: Module, function: Function) -> None:
        """Add the codes every function this one can call exits with or parses arguments for, followed through
        calls by name into its own module and the modules its imports name."""
        if id(function) in self.reached:
            return
        self.reached.add(id(function))
        for node in _own_nodes(function):
            if isinstance(node, ast.Raise) and node.exc is not None and not isinstance(node.exc, ast.Call):
                # A SystemExit raised as the class, with no code, exits 0.
                if qualified_name(node.exc, module.aliases) == "SystemExit":
                    self.reading.codes.add(0)
                continue
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == ERROR_CODE and (number := self.code_of(module, keyword.value)) is not None:
                    self.error_codes.add(number)
            name = qualified_name(node.func, module.aliases)
            if name in EXIT_CALLS:
                self.exits(call_argument(node, 0, "status" if name == "os._exit" else "code"), Scope(module, function))
            elif isinstance(node.func, ast.Attribute) and (
                node.func.attr in PARSING_CALLS
                or (node.func.attr == USAGE_ERROR_CALL and _is_parser(node.func.value, module.parsers))
            ):
                self.reading.codes.add(USAGE)
                self.parsing.add(module.path)
            named = self.function_named(module, node.func) if isinstance(node.func, (ast.Name, ast.Attribute)) else None
            if named is not None:
                self.reach(*named)


def runner_targets(block: ast.If, aliases: dict[str, str]) -> list[ast.expr]:
    """What each run_main call in a `__main__` block runs: its function, or the call a lambda makes."""
    targets: list[ast.expr] = []
    for node in ast.walk(block):
        if isinstance(node, ast.Call) and qualified_name(node.func, aliases) == RUNNER and node.args:
            target = node.args[0]
            targets.append(target.body if isinstance(target, ast.Lambda) else target)
    return targets


def read_codes(root: Path, entry: EntryPoint) -> CodeReader:
    """The reader of an entry point, having read the codes it can exit with and the returns it could not follow."""
    reader = CodeReader(root, entry.path)
    module = reader.module_at(entry.path, entry.tree)
    assert module is not None
    reader.reading.codes.add(RUNNER_FAILURE)
    scope = Scope(module, None)
    for block in main_blocks(entry.tree):
        for target in runner_targets(block, module.aliases):
            functions = None if isinstance(target, ast.Call) else reader.callables(target, scope)
            if functions is None:
                reader.value(target, scope)
            for each, function in functions or []:
                reader.returns(each, function, Scope(each, function))
    if reader.reads_error_code:
        reader.reading.codes |= reader.error_codes | reader.error_defaults()
    return reader


def table_problems(root: Path = REPOSITORY_ROOT, files: list[Path] | None = None) -> list[str]:
    """Each entry point whose docstring holds no exit table in the settled form, or one that does not name exactly
    the codes its code can exit with; each return the reader cannot follow that no allowance names; and each
    allowance that names no such return."""
    problems: list[str] = []
    met: set[tuple[Path, str]] = set()
    entries = entry_points(root, problems, files)
    if not entries:
        problems.append(
            f"the exit-table policy found no entry point, no module whose __main__ block calls {RUNNER}, so it read no "
            "exit table: the scan or the runner it looks for has moved"
        )
    for entry in entries:
        reader = read_codes(root, entry)
        own = reader.modules[entry.path].allowed
        for path, line, expression in reader.reading.unread:
            if expression in reader.modules[path].allowed:
                met.add((path, expression))
            elif expression in own:
                met.add((entry.path, expression))
            else:
                problems.append(
                    f"{path.relative_to(root).as_posix()}:{line} returns or exits with {expression}, an exit code "
                    f"the exit-table reader cannot follow from {entry.name}; return a number, a module constant, or a "
                    f"function's result, or name it in {ALLOWANCE} with where its codes come from; see {RESULTS_DOC}"
                )
        try:
            table = exit_table(ast.get_docstring(entry.tree))
        except TableUnreadable as error:
            problems.append(f"{entry.name} {error}; see {RESULTS_DOC}")
            continue
        expected = reader.reading.codes - {INTERRUPTED}
        problems += [
            f"{entry.name} can exit {code}, which its exit table does not name; see {RESULTS_DOC}"
            for code in sorted(expected - table.keys())
        ]
        # An entry point that reaches a return the reader cannot follow, an allowed one included, may name codes the
        # reader does not see, so its table is held only to name each code the reader does see.
        if not reader.reading.unread:
            problems += [
                f"{entry.name} names exit {code} in its exit table, which no path of its code exits with; see "
                f"{RESULTS_DOC}"
                for code in sorted(table.keys() - expected - {INTERRUPTED})
            ]
    problems += [
        f"{path.relative_to(root).as_posix()} names {expression!r} in {ALLOWANCE}, which is no return the exit-table "
        "reader cannot follow from an entry point; remove it"
        for path, allowed in sorted(allowances(root, files, problems).items())
        for expression in allowed
        if (path, expression) not in met
    ]
    return problems


def allowances(root: Path, files: list[Path] | None, problems: list[str]) -> dict[Path, dict[str, str]]:
    """Every module's EXIT_CODES_ALLOWED, read in each Python file the policy reads, whether or not an entry point
    reaches it, so an allowance no return needs any longer fails wherever it is left; one that is no literal
    dictionary of non-empty reasons, as module_allowance reads every policy's allowance, is a problem."""
    found: dict[Path, dict[str, str]] = {}
    for path in scanned_files(root, files):
        try:
            if ALLOWANCE not in python_source(path):
                continue
            module = Module.read(path, parse_python(path))
        except (UnreadablePython, OSError):
            continue
        if module.allowance_malformed:
            problems.append(
                f"{path.relative_to(root).as_posix()}: {ALLOWANCE} must map each expression to where its codes come "
                "from, a non-empty reason"
            )
        if module.allowed:
            found[path] = module.allowed
    return found


class ExitTablePolicies(unittest.TestCase):
    def test_each_entry_point_s_exit_table_names_exactly_the_codes_it_can_exit_with(self) -> None:
        problems = table_problems()
        self.assertEqual(problems, [], "\n".join(problems))

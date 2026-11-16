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
import functools
import re
import unittest
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import file_presence
import python_reader
import whole_number
from exit_calls import EXIT_CALLS, NO_CODE, SYSTEM_EXIT, code_keyword, fixed_code
from path_containment import within
from skill_scripts import PARSER_CLASS, RESULTS_DOC, _is_parser, binding, main_blocks
from text_lines import text_lines
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
# read as the usage exit only on a receiver skill_scripts reads as a parser, one the module binds to a parser, or, in
# a method, an attribute of its receiver that its class's body binds to a parser.
USAGE_ERROR_CALL = "error"
# The decorator that makes a method static, as a bare name and as resolved through an import of builtins.
STATIC_METHOD = frozenset({"staticmethod", "builtins.staticmethod"})
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
    lines = text_lines(docstring or "")
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
    # The names it binds to an argument parser, in any scope, and the receiver's attributes each method reaches one by.
    parsers: set[str] = field(default_factory=set)
    method_parsers: dict[Function, set[str]] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path, tree: ast.Module, package: str = "") -> Module:
        module = cls(path, tree, aliases={**relative_aliases(tree, package), **import_aliases(tree)})
        module.parsers = parser_names(tree, module.aliases)
        module.method_parsers = method_parsers(tree, module.aliases)
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
    """Whether an annotation names argparse's parser, as a name or a quoted one. A stated limit: a quoted annotation
    that does not parse or nests past nesting_depth's DEPTH_LIMIT is read as naming nothing."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = python_reader.parse_expression(annotation.value).body
        except python_reader.PythonRefused:
            return False
    return annotation is not None and qualified_name(annotation, aliases) == PARSER_CLASS


def _parser_targets(node: ast.AST, aliases: dict[str, str], builders: set[str]) -> list[ast.expr]:
    """The targets one statement binds to an argument parser: one annotated as a parser, or one assigned
    argparse.ArgumentParser(...), a subcommand's add_parser(...), or a call of a function of the module annotated to
    return a parser."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return []
    targets: list[ast.expr] = [*node.targets] if isinstance(node, ast.Assign) else [node.target]
    if isinstance(node, ast.AnnAssign) and is_parser_annotation(node.annotation, aliases):
        return targets
    if not isinstance(node.value, ast.Call):
        return []
    callee = node.value.func
    if (
        qualified_name(callee, aliases) == PARSER_CLASS
        or (isinstance(callee, ast.Attribute) and callee.attr == SUBPARSER_CALL)
        or (isinstance(callee, ast.Name) and callee.id in builders)
    ):
        return targets
    return []


def _parser_builders(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """The functions of a module, in any scope, annotated to return an argument parser."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_parser_annotation(node.returns, aliases)
    }


def parser_names(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """The names and attribute chains, such as self.cli, a module binds to an argument parser, in any scope and as
    written at the binding: one assigned argparse.ArgumentParser(...), a subcommand's add_parser(...), or a call of
    its own function annotated to return a parser, and a parameter, name, or attribute annotated as one."""
    builders = _parser_builders(tree, aliases)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and is_parser_annotation(node.annotation, aliases):
            found.add(node.arg)
        found.update(binding(target) for target in _parser_targets(node, aliases, builders))
    return found - {""}


def _class_scope(owner: ast.ClassDef) -> Iterator[ast.AST]:
    """Every node of a class's own scope: its body's statements, at any depth of an if, try, with, or loop, and the
    functions and classes it defines, never what one of those defines inside itself."""
    stack: list[ast.AST] = list(owner.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def method_parsers(tree: ast.Module, aliases: dict[str, str]) -> dict[Function, set[str]]:
    """The attribute chains on its own receiver each method reaches a parser by. A name a class binds to a parser in
    its own scope, as `cli: argparse.ArgumentParser` or `cli = argparse.ArgumentParser()`, under an if, try, or with
    as well, is reached in each method that class's scope defines as an attribute of the method's first parameter,
    such as self.cli or cls.cli, and in no other class's method; a static method has no receiver."""
    builders = _parser_builders(tree, aliases)
    found: dict[Function, set[str]] = {}
    for owner in ast.walk(tree):
        if not isinstance(owner, ast.ClassDef):
            continue
        scope = list(_class_scope(owner))
        names = {
            target.id
            for statement in scope
            for target in _parser_targets(statement, aliases, builders)
            if isinstance(target, ast.Name)
        }
        for method in scope:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or any(
                qualified_name(decorator, aliases) in STATIC_METHOD for decorator in method.decorator_list
            ):
                continue
            receiver = [*method.args.posonlyargs, *method.args.args][:1]
            if receiver:
                found[method] = {f"{receiver[0].arg}.{name}" for name in names}
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


@dataclass(frozen=True, eq=False)
class Scope:
    """Where an expression is read: its module, the function that holds it, and the expression each of that function's
    parameters is given at the call the scope stands for. The reader keeps one scope for each function, each set of
    argument expressions it is given, which a call site fixes, and each item it is read for, and reads a parameter as
    every value the scopes that called it give it: see CodeReader.scope."""

    module: Module
    function: Function | None
    given: tuple[tuple[str, ast.expr], ...]

    def argument(self, name: str) -> ast.expr | None:
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


def _bindings(function: Function) -> dict[str, list[tuple[ast.expr, int | None]]]:
    """Each value a function's own body binds to each name, by assignment or annotated assignment, with the position
    the name takes when it is one of a tuple's targets, as `code` in `code, lines = run()`."""
    found: dict[str, list[tuple[ast.expr, int | None]]] = {}
    for node in _own_nodes(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append((node.value, None))
                elif isinstance(target, ast.Tuple):
                    for index, each in enumerate(target.elts):
                        if isinstance(each, ast.Name):
                            found.setdefault(each.id, []).append((node.value, index))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found.setdefault(node.target.id, []).append((node.value, None))
    return found


def _falls_through(body: list[ast.stmt], aliases: dict[str, str]) -> bool:
    """Whether a block can end without a return or a raise, read by its last statement: an exit call, by the dotted
    name the module's imports resolve it to, ends it, and a method of the same name, such as `smtp.quit()`, does not."""
    if not body:
        return True
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return False
    if isinstance(last, ast.If):
        return _falls_through(last.body, aliases) or _falls_through(last.orelse, aliases)
    if isinstance(last, ast.Try):
        ends = [last.body if not last.orelse else last.orelse, *(handler.body for handler in last.handlers)]
        return any(_falls_through(block, aliases) for block in ends) and _falls_through(last.finalbody, aliases)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _falls_through(last.body, aliases)
    if isinstance(last, ast.Match):
        return True
    return not (
        isinstance(last, ast.Expr)
        and isinstance(last.value, ast.Call)
        and qualified_name(last.value.func, aliases) in EXIT_CALLS - {SYSTEM_EXIT}
    )


@dataclass
class Reading:
    """What the reader found from one entry point: the codes it can exit with, and the returns it could not follow,
    each by its file, line, and expression."""

    codes: set[int] = field(default_factory=set)
    unread: list[tuple[Path, int, str]] = field(default_factory=list)


def _distinct(lists: Iterable[list[tuple[Module, Function]]]) -> list[tuple[Module, Function]]:
    """The functions the lists name, each once, in the order first named."""
    return list({id(function): (module, function) for found in lists for module, function in found}.values())


@dataclass(frozen=True)
class Parts:
    """What one called expression is made of, as CodeReader.callables reads it: the function it names; the values it
    is read as, each with the scope it is read in; whether those are the values of a parameter, one for each scope
    that called its own; and whether the reader can tell it at all."""

    named: tuple[Module, Function] | None = None
    values: list[tuple[ast.expr, Scope]] = field(default_factory=list)
    callers: bool = False
    known: bool = True


class CodeReader:
    """Reads the codes one entry point can exit with, following into the modules its imports name."""

    def __init__(self, root: Path, entry: Path) -> None:
        self.root = root
        self.folders = [entry.parent, root / SKILL_CORE_SCRIPTS, root]
        self.modules: dict[Path, Module] = {}
        self.reading = Reading()
        # Each scope the reader keeps, by its module, its function, and the argument expressions it is given, and the
        # scopes that called each, by the scope's id: see scope.
        self.scopes: dict[tuple[int, int, tuple[tuple[str, int], ...], int | None], Scope] = {}
        self.callers: dict[int, dict[int, Scope]] = {}
        # What one pass has read, each once: a function's returns for each element in a scope, a local's value for
        # each element in a scope, and a parameter for each element in a scope; and the scopes a read asked the
        # callers of. A caller that reaches a scope after a read asked its callers makes the pass late: see read.
        self.returned: set[tuple[int | None, int]] = set()
        self.assigned_read: set[tuple[int, int | None, int]] = set()
        self.parameters_read: set[tuple[int, str, int | None]] = set()
        self.asked: set[int] = set()
        self.late: bool
        self.passes = 0
        # The reads waiting their turn, so no read nests inside another on Python's stack: see later.
        self.pending: list[Callable[[], None]] = []
        self.unread_seen: set[tuple[Path, int, str]] = set()
        # Each function's bindings, by the function's id: see bound.
        self.bindings: dict[int, dict[str, list[tuple[ast.expr, int | None]]]] = {}
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

    def scope(
        self,
        module: Module,
        function: Function | None,
        given: tuple[tuple[str, ast.expr], ...],
        caller: Scope | None,
        element: int | None = None,
    ) -> Scope:
        """The one scope of a function given these argument expressions and read for this item of the tuple it
        returns, or for the whole value, with the scope that called it recorded.

        A scope stands for every call that gives its function the same argument expressions, which one call site
        fixes, read for the same item, as returns reads a function once for each item. So the reader keeps no more
        scopes than the calls the source holds, each once for each item it is read for, plus one for each function it
        runs given nothing (what run_main runs, a property, a function an exit is read in) and one for each module's
        own body, however the calls recurse and whatever values reach them: never a product over a function's
        parameters of the values reaching each. A parameter is read as the expression it is given, in every scope
        recorded as calling its scope, so two calls of one function given the same expressions in different scopes, as
        `leaf(x)` in `mid(3)`'s scope and in `mid(4)`'s, are both read, and every value the first scope to call it
        gives is read among them. A caller recorded after a read of the pass asked a scope's callers makes the pass
        late: see read.

        A stated limit: a parameter is read as the values every call with the same expressions gives it, so where one
        argument chooses the function a call runs and another gives that function's value, a value may be read with a
        function it is never given, and a code read that the program cannot exit with; no repository code passes a
        function and its value apart like that."""
        key = (id(module), id(function), tuple((name, id(value)) for name, value in given), element)
        made = self.scopes.get(key)
        if made is None:
            made = self.scopes[key] = Scope(module, function, given)
        callers = self.callers.setdefault(id(made), {})
        if caller is not None and id(caller) not in callers:
            callers[id(caller)] = caller
            if id(made) in self.asked:
                self.late = True
        return made

    def module_scope(self, module: Module) -> Scope:
        """The one scope of a module's own body."""
        return self.scope(module, None, (), None)

    def callers_of(self, scope: Scope) -> list[Scope]:
        """The scopes that called a scope so far, recording that a read asked."""
        self.asked.add(id(scope))
        return list(self.callers[id(scope)].values())

    def _parts(self, node: ast.expr, scope: Scope) -> Parts:
        """What one called expression is made of: the function it names, the values it is read as, or none it can
        tell."""
        named = self.function_named(scope.module, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if named is not None:
            return Parts(named=named)
        if isinstance(node, ast.Name):
            argument = scope.argument(node.id)
            values = self.bound(scope.function, node.id) if scope.function is not None else []
            if argument is not None:
                return Parts(values=[(argument, caller) for caller in self.callers_of(scope)], callers=True)
            if values and all(position is None for _, position in values):
                return Parts(values=[(value, scope) for value, _ in values])
            if node.id in scope.module.dictionaries:
                module_scope = self.module_scope(scope.module)
                return Parts(values=[(value, module_scope) for value in scope.module.dictionaries[node.id].values])
            return Parts(known=False)
        if isinstance(node, ast.Subscript):
            return Parts(values=[(node.value, scope)])
        if isinstance(node, ast.Dict):
            return Parts(values=[(value, scope) for value in node.values])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            return Parts(values=[(node.func.value, scope)])
        return Parts(known=False)

    def callables(self, node: ast.expr, scope: Scope) -> tuple[list[tuple[Module, Function]], bool]:
        """The functions a called expression can be, and whether the reader can tell them all.

        Each expression is answered once for the question in each scope, from a stack rather than Python's, and one
        read again while its own answer is open leads back to itself and adds nothing, so a local rebound from itself,
        as `handler = handler.get("a")`, a table of functions that names itself, or a parameter a recursion passes on,
        ends, since the functions are their union. Within one scope, a value the reader cannot tell among a local's or
        a table's values leaves the whole answer untold, as a call through it can be any of them. A parameter is
        answered in each scope that called its own: where the reader can tell it in some of them, it is the functions
        those name, and one it cannot tell leaves the question incomplete, so the call is unread and those functions
        are still read; where it can tell it in none, the parameter is untold, as a local's value is."""
        root = (id(node), id(scope))
        answers: dict[tuple[int, int], tuple[list[tuple[Module, Function]], bool]] = {}
        parts: dict[tuple[int, int], Parts] = {}
        incomplete = False
        stack: list[tuple[ast.expr, Scope, bool]] = [(node, scope, False)]
        while stack:
            node, scope, ready = stack.pop()
            key = (id(node), id(scope))
            if not ready:
                if key in answers:
                    continue
                answers[key] = ([], True)
                made = parts[key] = self._parts(node, scope)
                stack.append((node, scope, True))
                stack += [(value, given, False) for value, given in made.values]
                continue
            made = parts[key]
            results = [answers[(id(value), id(given))] for value, given in made.values]
            if made.named is not None:
                answers[key] = ([made.named], True)
            elif not made.known:
                answers[key] = ([], False)
            elif made.callers and any(known for _, known in results):
                incomplete = incomplete or not all(known for _, known in results)
                answers[key] = (_distinct(found for found, _ in results), True)
            else:
                known = all(each for _, each in results)
                answers[key] = (_distinct(found for found, _ in results) if known else [], known)
        found, known = answers[root]
        return found, known and not incomplete

    def call_scope(
        self, call: ast.Call, module: Module, function: Function, scope: Scope, element: int | None
    ) -> Scope:
        """The scope a followed call runs its function in, each parameter given the argument the call passes, called
        from the scope the call is read in."""
        parameters = [*function.args.posonlyargs, *function.args.args]
        given = [
            (parameter.arg, argument)
            for parameter, argument in zip(parameters, call.args, strict=False)
            if not isinstance(argument, ast.Starred)
        ]
        given += [(keyword.arg, keyword.value) for keyword in call.keywords if keyword.arg is not None]
        return self.scope(module, function, tuple(given), scope, element)

    def bound(self, function: Function, name: str) -> list[tuple[ast.expr, int | None]]:
        """Each value a function's own body binds to a name, read from the function once."""
        if id(function) not in self.bindings:
            self.bindings[id(function)] = _bindings(function)
        return self.bindings[id(function)].get(name, [])

    def later(self, read: Callable[..., None], *arguments: object) -> None:
        """Queue a read of a function, a local, or a parameter, which read runs: see read."""
        self.pending.append(functools.partial(read, *arguments))

    def parameter(self, scope: Scope, name: str, element: int | None) -> None:
        """Add the codes a parameter can be: the expression it is given, read in each scope that called its scope."""
        key = (id(scope), name, element)
        if key in self.parameters_read:
            return
        self.parameters_read.add(key)
        argument = scope.argument(name)
        for caller in self.callers_of(scope):
            self.value(argument, caller, element)

    def returns(self, module: Module, function: Function, scope: Scope, element: int | None = None) -> None:
        """Add the codes a function returns, or the item at `element` of the tuples it returns, to the reading."""
        key = (element, id(scope))
        if key in self.returned:
            return
        self.returned.add(key)
        self.reach(module, function)
        for node in _own_nodes(function):
            if isinstance(node, ast.Return):
                self.value(node.value, scope, element)
        if _falls_through(function.body, module.aliases) and element is None:
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
        elif isinstance(node, ast.Name) and scope.argument(node.id) is not None:
            self.later(self.parameter, scope, node.id, element)
        elif isinstance(node, ast.Name) and scope.function is not None and self.bound(scope.function, node.id):
            self.later(self.assigned, node, scope, element)
        elif isinstance(node, ast.Call):
            functions, complete = self.callables(node.func, scope)
            for module, function in functions:
                self.later(
                    self.returns, module, function, self.call_scope(node, module, function, scope, element), element
                )
            if not complete:
                self._unread(node, scope)
        elif isinstance(node, ast.Attribute) and node.attr in scope.module.properties:
            for method in scope.module.properties[node.attr]:
                self.later(self.returns, scope.module, method, self.scope(scope.module, method, (), scope), element)
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
        """Add the codes each value the function binds to a local name can be.

        Each value is read once a pass for each element and each scope, as returns reads a function: a value that
        reads its own name, as `code = code if flag else 2`, leads back to itself, and adds no code its first
        read has not, since the codes are their union."""
        assert scope.function is not None
        for value, position in self.bound(scope.function, node.id):
            if position is not None and element is not None:
                self._unread(node, scope)
                continue
            key = (id(value), element if position is None else position, id(scope))
            if key in self.assigned_read:
                continue
            self.assigned_read.add(key)
            self.value(value, scope, element if position is None else position)

    def _unread(self, node: ast.expr | None, scope: Scope) -> None:
        expression = ast.unparse(node) if node is not None else "None"
        found = (scope.module.path, getattr(node, "lineno", 0), expression)
        if found not in self.unread_seen:
            self.unread_seen.add(found)
            self.reading.unread.append(found)

    def exits(self, argument: ast.expr | None, scope: Scope) -> None:
        """Add the codes an exit call or a raised SystemExit is given: no argument or None exits 0, a string is printed
        and exits 1, as exit_calls.fixed_code reads them, and any other argument is read as a returned expression is,
        so one the reader cannot follow is recorded as unread."""
        if (code := fixed_code(argument)) is not None:
            self.reading.codes.add(code)
        else:
            self.value(argument, scope)

    def reach(self, module: Module, function: Function) -> None:
        """Add the codes every function this one can call exits with or parses arguments for, followed through
        calls by name into its own module and the modules its imports name, from a stack rather than Python's."""
        stack = [(module, function)]
        while stack:
            module, function = stack.pop()
            if id(function) in self.reached:
                continue
            self.reached.add(id(function))
            for node in _own_nodes(function):
                if isinstance(node, ast.Raise) and node.exc is not None and not isinstance(node.exc, ast.Call):
                    # A SystemExit raised as the class, with no code, exits 0.
                    if qualified_name(node.exc, module.aliases) == SYSTEM_EXIT:
                        self.reading.codes.add(NO_CODE)
                    continue
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == ERROR_CODE and (number := self.code_of(module, keyword.value)) is not None:
                        self.error_codes.add(number)
                name = qualified_name(node.func, module.aliases)
                if name in EXIT_CALLS:
                    self.exits(call_argument(node, 0, code_keyword(name)), self.scope(module, function, (), None))
                elif isinstance(node.func, ast.Attribute) and (
                    node.func.attr in PARSING_CALLS
                    or (
                        node.func.attr == USAGE_ERROR_CALL
                        and _is_parser(node.func.value, module.parsers | module.method_parsers.get(function, set()))
                    )
                ):
                    self.reading.codes.add(USAGE)
                    self.parsing.add(module.path)
                if isinstance(node.func, (ast.Name, ast.Attribute)) and (
                    named := self.function_named(module, node.func)
                ):
                    stack.append(named)

    def read(self, entry: Module) -> None:
        """Read what the entry point's run_main runs, a pass at a time, until a pass ends with no late caller.

        Each read of a pass waits its turn in `pending` and runs from there, so however deep the calls run, no read
        nests inside another on Python's stack, only an expression's parts, which the parser bounds. A pass reads each
        function, local, and parameter once for each element and scope. A caller that reaches a scope after a read of
        that pass asked the scope's callers may give it a value that read missed, so the pass is late and another
        follows, with every caller recorded so far; each late pass records a caller no pass had, and the callers are
        bounded by the scopes, so the passes end."""
        while True:
            self.passes += 1
            self.returned.clear()
            self.assigned_read.clear()
            self.parameters_read.clear()
            self.asked.clear()
            self.reached.clear()
            self.late = False
            scope = self.module_scope(entry)
            for block in main_blocks(entry.tree):
                for target in runner_targets(block, entry.aliases):
                    functions, complete = ([], False) if isinstance(target, ast.Call) else self.callables(target, scope)
                    if not complete:
                        self.value(target, scope)
                    for each, function in functions:
                        self.later(self.returns, each, function, self.scope(each, function, (), scope))
            while self.pending:
                self.pending.pop()()
            if not self.late:
                return


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
    reader.read(module)
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

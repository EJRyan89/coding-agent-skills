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

A row's code is a whole number; the rows run in ascending order, each code once. The table names every code the module
can exit with, and no other: 1, which run_main exits with on a failure no handler caught; 2 when the code it reaches
parses arguments with argparse, whose usage error exits 2; each code an exit call or a raised SystemExit is given, in
any function the entry point reaches, where none or None exits 0, a string exits 1, and any other argument is read as a
return is; and each code the function run_main runs returns, read through what it returns: a number, a constant bound to
numbers, as ejryan89-skill-core's exit_constants reads one in its own module or one an import names, either branch of a
conditional, a local name bound to one of those, and the result of a function it calls, a function of its own module or
one an import names, by name, through a parameter given a function, or through a table of functions it indexes or looks
up. A function that ends without a return returns None, which exits 0. Ctrl+C's 130 is run_main's alike for every entry
point, stated once in "Script results", so a table names it only where the code gives it a meaning of its own.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import exit_constants
import file_presence
import whole_number
from exit_calls import EXIT_CALLS, NO_CODE, SYSTEM_EXIT, call_code, exit_argument
from path_containment import within
from skill_scripts import RESULTS_DOC, ParserBindings, Spellings, main_blocks, parser_names
from text_lines import text_lines
from validation_support import (
    REPOSITORY_ROOT,
    UnreadablePython,
    import_aliases,
    is_test_script,
    module_allowance,
    parse_python,
    python_source,
    qualified_name,
    relative_import_aliases,
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
# read as the usage exit only on a receiver skill_scripts.parser_names reads as a parser where the call sits, the one
# reader the script-results policy reads parser.error by too.
USAGE_ERROR_CALL = "error"
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
    # Its int constants, as ejryan89-skill-core's exit_constants reads them.
    constants: exit_constants.ModuleConstants = field(
        default_factory=lambda: exit_constants.ModuleConstants({}, frozenset(), {})
    )
    dictionaries: dict[str, ast.Dict] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, str] = field(default_factory=dict)
    # Whether its EXIT_CODES_ALLOWED is no literal dictionary of non-empty reasons, as module_allowance reads one.
    allowance_malformed: bool = False
    # Each property a class of the module defines, by its name.
    properties: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = field(default_factory=dict)
    # What it binds to an argument parser, as skill_scripts.parser_names reads it.
    parsers: ParserBindings = field(default_factory=lambda: ParserBindings(Spellings(), {}))

    @classmethod
    def read(cls, path: Path, tree: ast.Module, package: str = "") -> Module:
        module = cls(path, tree, aliases={**relative_import_aliases(tree, package), **import_aliases(tree)})
        module.parsers = parser_names(tree, module.aliases)
        module.constants = exit_constants.read(tree, module.aliases)
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
                if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
                    module.dictionaries[target.id] = value
        allowed = module_allowance(tree, ALLOWANCE)
        module.allowed = allowed or {}
        module.allowance_malformed = allowed is None
        return module


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
    each by its file, line, and expression, in the order of their place in the source (see read_codes)."""

    codes: set[int] = field(default_factory=set)
    unread: list[tuple[Path, int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Parts:
    """What one called expression is made of, as CodeReader.callables reads it: the function it names; the values it
    is read as, each with the scope it is read in; whether those are the values of a parameter, one for each scope
    that called its own; and whether the reader can tell it at all."""

    named: tuple[Module, Function] | None = None
    values: list[tuple[ast.expr, Scope]] = field(default_factory=list)
    callers: bool = False
    known: bool = True


# A called expression as callables keys it: the expression's id and the id of the scope it is read in; and as its
# walk enters it: the expression, its parts, and its scope.
Key = tuple[int, int]
Made = tuple[ast.expr, Parts, Scope]


@dataclass(eq=False)
class Asking:
    """One read that asked the callers of a scope, kept with what it has done, so the pass after a late caller runs it
    again on what is new: a parameter's read, with how many of its scope's callers it has read, and a call's or a
    run_main target's, with the functions it has followed. See CodeReader.read."""

    run: Callable[[Asking], None]
    scope: Scope
    element: int | None = None
    node: ast.expr | None = None
    name: str = ""
    seen: int = 0
    followed: set[int] = field(default_factory=set)


@dataclass(eq=False, repr=False)
class Group:
    """A strongly connected group of called expressions that CodeReader.callables answered together: its members, by
    their keys; the groups whose answers read one of its members' answers; and the reads that asked for one of them.
    A late caller of a scope whose callers a member asked drops the group, every group that read it, and their answers,
    and the pass that follows runs their reads again: see CodeReader.again. It has no field-by-field repr, since its
    readers and reads lead to each other's groups, which such a repr writes out once on each path to them."""

    members: list[Key]
    readers: list[Group] = field(default_factory=list)
    askings: dict[int, Asking] = field(default_factory=dict)
    dropped: bool = False
    # Whether a group it reads was dropped with it; the groups outside it whose answers its members read; and, for a
    # group of one parameter, that parameter as read in its scope and how many of the scope's callers it read: see
    # CodeReader._grow.
    read_dropped: bool = False
    reads: dict[int, Group] = field(default_factory=dict)
    parameter: tuple[ast.expr, Scope, int] | None = None


@dataclass(frozen=True)
class Answer:
    """What callables answers for one called expression in one scope: the functions it can be, whether the reader can
    tell it, and whether it can tell every value the expression leads to; and the group it was answered in."""

    group: Group
    functions: dict[int, tuple[Module, Function]]
    told: bool
    complete: bool


def _told(
    parts: dict[Key, Parts], values: dict[Key, list[Key]], readers: dict[Key, list[Key]], outside: dict[Key, Answer]
) -> dict[Key, bool]:
    """Which members of a group the reader can tell, the greatest such set: a parameter is told when one of its
    values is, and any other expression the reader can read when all its values are, each member inside the group
    taken as told until a value it cannot tell makes it not."""
    told = dict.fromkeys(parts, True)
    # For a parameter, how many of its values are told; any other expression is untold once one value is not.
    counts: dict[Key, int] = {}
    untold: list[Key] = []
    for member, made in parts.items():
        results = [child not in outside or outside[child].told for child in values[member]]
        if made.callers:
            counts[member] = sum(results)
            if results and not counts[member]:
                untold.append(member)
        elif made.named is None and (not made.known or not all(results)):
            untold.append(member)
    for member in untold:
        told[member] = False
    while untold:
        member = untold.pop()
        for reader in readers[member]:
            if not told[reader]:
                continue
            if parts[reader].callers:
                counts[reader] -= 1
                if counts[reader]:
                    continue
            told[reader] = False
            untold.append(reader)
    return told


def _functions(
    parts: dict[Key, Parts],
    values: dict[Key, list[Key]],
    readers: dict[Key, list[Key]],
    outside: dict[Key, Answer],
    told: dict[Key, bool],
) -> dict[Key, dict[int, tuple[Module, Function]]]:
    """The functions of each told member of a group, the least such sets: the function it names, and those of each
    told value it leads to; an untold member has none."""
    functions: dict[Key, dict[int, tuple[Module, Function]]] = {member: {} for member in parts}
    for member, made in parts.items():
        if not told[member]:
            continue
        if made.named is not None:
            functions[member][id(made.named[1])] = made.named
        for child in values[member]:
            if child in outside:
                functions[member].update(outside[child].functions)
    changed = [member for member in parts if told[member]]
    while changed:
        member = changed.pop()
        for reader in readers[member]:
            if told[reader] and not functions[member].keys() <= functions[reader].keys():
                functions[reader].update(functions[member])
                changed.append(reader)
    return functions


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
        # What the reader has read, each once whatever pass reached it: a function's returns for each element in a
        # scope, a local's value for each element in a scope, and a parameter for each element in a scope, with the
        # parameter reads of each scope; and each read of a call for an element in a scope. The scopes a read asked the
        # callers of, and those a caller reached after that, which the next pass reads again for: see read.
        self.returned: set[tuple[int | None, int]] = set()
        self.assigned_read: set[tuple[int, int | None, int]] = set()
        self.parameters_read: dict[tuple[int, str, int | None], Asking] = {}
        self.parameter_askings: dict[int, list[Asking]] = {}
        self.call_askings: dict[tuple[int, int, int | None], Asking] = {}
        self.asked: set[int] = set()
        self.late: set[int] = set()
        self.passes = 0
        # callables' answer for each called expression in each scope, and the groups that asked each scope's callers.
        self.answers: dict[Key, Answer] = {}
        self.groups_asking: dict[int, list[Group]] = {}
        # The reads waiting their turn, so no read nests inside another on Python's stack: see later.
        self.pending: list[Callable[[], None]] = []
        # Each unread return recorded, with the first column it was found at on its line: see read_codes.
        self.unread_seen: dict[tuple[Path, int, str], int] = {}
        # Each function's bindings, by the function's id: see bound.
        self.bindings: dict[int, dict[str, list[tuple[ast.expr, int | None]]]] = {}
        self.reached: set[int] = set()
        # Whether a return reads an error's exit_code, and each code a reached call passes as exit_code.
        self.reads_error_code = False
        self.error_codes: set[int] = set()
        # Each exit_code keyword or default whose code the reader cannot read, with its module, recorded as unread
        # where a return reads an error's exit_code: see read_codes.
        self.error_unread: list[tuple[Module, ast.expr]] = []
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

    def code_of(self, module: Module, node: ast.expr) -> frozenset[int] | None:
        """The numbers an expression is: a literal, or a name's constant as ejryan89-skill-core's exit_constants
        resolves it, in its own module or in one an import names, which module_named finds."""
        return exit_constants.resolve(node, module.constants, self.constants_named)

    def error_defaults(self) -> set[int]:
        """Each default a parameter named exit_code takes in a module the reader followed, recording one it cannot
        read in error_unread."""
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
                for parameter, default in pairs:
                    if parameter.arg == ERROR_CODE and default is not None:
                        found |= self.error_code(module, default) or set()
        return found

    def error_code(self, module: Module, node: ast.expr) -> frozenset[int] | None:
        """The codes an exit_code keyword or default gives, or None, recording it in error_unread, when the reader
        cannot read it. A parameter named exit_code passed on as itself, or another error's exit_code passed on, as
        `exit_code=refusal.exit_code`, gives nothing of its own: each default and keyword that sets one is read where
        it is written."""
        if (isinstance(node, ast.Name) and node.id == ERROR_CODE) or (
            isinstance(node, ast.Attribute) and node.attr == ERROR_CODE
        ):
            return None
        numbers = self.code_of(module, node)
        if numbers is None:
            self.error_unread.append((module, node))
        return numbers

    def constants_named(self, dotted: str) -> exit_constants.ModuleConstants | None:
        """The constants of the module a dotted name names, as module_named finds it."""
        other = self.module_named(dotted)
        return other.constants if other is not None else None

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
        fixes, read for the same item, as returns reads a function once for each item. A call site that can name
        several functions, through a table of them or a parameter given several, makes a scope for each. So the reader
        keeps no more scopes than the call sites the source holds, times the functions each can name, times the items
        each is read for, plus one for each function it runs given nothing (what run_main runs, a property, a function
        an exit is read in) and one for each module's own body, however the calls recurse and whatever values reach
        them: a number polynomial in the source, never a product over a function's parameters of the values reaching
        each. A parameter is read as the expression it is given, in every scope
        recorded as calling its scope, so two calls of one function given the same expressions in different scopes, as
        `leaf(x)` in `mid(3)`'s scope and in `mid(4)`'s, are both read, and every value the first scope to call it
        gives is read among them. A caller recorded after a read of the pass asked a scope's callers makes the pass
        late: see read.

        A stated limit: a parameter is read as the values every call with the same expressions gives it, so where one
        argument chooses the function a call runs and another gives that function's value, a value may be read with a
        function it is never given, and a code read that the program cannot exit with. The policy then refuses a table
        that names every code the program can exit with, which main accepted, and the only way through is to name the
        code it cannot exit with, since EXIT_CODES_ALLOWED excuses only a return the reader cannot follow. No
        repository code calls a function it is given with a value that function returns as its exit code."""
        key = (id(module), id(function), tuple((name, id(value)) for name, value in given), element)
        made = self.scopes.get(key)
        if made is None:
            made = self.scopes[key] = Scope(module, function, given)
        callers = self.callers.setdefault(id(made), {})
        if caller is not None and id(caller) not in callers:
            callers[id(caller)] = caller
            if id(made) in self.asked:
                self.late.add(id(made))
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

    def callables(self, node: ast.expr, scope: Scope, asking: Asking) -> tuple[list[tuple[Module, Function]], bool]:
        """The functions a called expression can be, and whether the reader can tell them all, recording that the read
        asked so it runs again when the answer can change.

        An expression leads to the values it is read as (see _parts). It names a function, is one the reader cannot
        tell, or is told as its values are: a parameter, answered in each scope that called its own, when any of them
        is told, and any other expression, a local's values or a table's, when all of them are, so within one scope a
        value the reader cannot tell leaves the whole answer untold, as a call through it can be any of them. Its
        functions are those of its told values. A call is complete when no value it leads to, however far, is one the
        reader cannot tell; otherwise it is unread and the functions found are still read. Values that lead to each
        other, as a local rebound from itself (`handler = handler.get("a")`), a table that names itself, or a
        parameter a recursion passes on, are answered together, a strongly connected group at a time: the greatest
        set of them told, and the least sets of functions. An answer is kept for every call in every pass, and dropped
        only when a late caller reaches a scope whose callers a member of its group asked, or a group it read is
        dropped: see read. So a call through a parameter of functions that call each other walks their callers once,
        not once for each call.

        As callers are added, an answer only grows: a parameter is told once any value is, which more callers cannot
        undo, since a scope given arguments is made by a call that records its caller, so no parameter is asked with
        none; its functions only gain; and a call only goes from complete to not. read relies on this.

        These are the answers a walk gives that answers each call afresh, taking a value it meets again while that
        value's answer is open as told with no functions: under either, a call is complete exactly when no value the
        reader cannot tell is reachable from it, since such a value leaves each expression above it on the walk untold
        or the call incomplete; and a complete call's functions are exactly the functions reachable from it, since
        every value it meets is told, so each function reaches the call through the expressions the walk read it from.
        A stated limit: for a call that is not complete, such a walk's functions depended on the order it met the
        values of a group in, where a value it met again while open was one that leads to a value the reader cannot
        tell, taken as told; it could then read a function these answers do not, or miss one, and these answers depend
        on no order. The call is unread either way, so its entry point's table is held only to name the codes the
        reader reads. No repository code reaches such a group."""
        key = (id(node), id(scope))
        if key not in self.answers:
            self._answer(node, scope)
        answer = self.answers[key]
        answer.group.askings[id(asking)] = asking
        return list(answer.functions.values()), answer.complete

    def _answer(self, node: ast.expr, scope: Scope) -> None:
        """Answer a called expression in its scope, and each expression it leads to that has no answer, a strongly
        connected group at a time, by Tarjan's walk from a stack of its own."""
        order: dict[Key, int] = {}
        low: dict[Key, int] = {}
        made: dict[Key, Made] = {}
        # The expressions entered whose group is not answered yet, with where each was entered among them, and the
        # walk's frames: an expression's key, its parts, and the next of its values to visit.
        entered: list[Key] = []
        places: dict[Key, int] = {}
        frames: list[tuple[Key, Parts, list[int]]] = []

        def enter(node: ast.expr, scope: Scope) -> None:
            key = (id(node), id(scope))
            order[key] = low[key] = len(order)
            parts = self._parts(node, scope)
            made[key] = (node, parts, scope)
            places[key] = len(entered)
            entered.append(key)
            frames.append((key, parts, [0]))

        enter(node, scope)
        while frames:
            key, parts, at = frames[-1]
            if at[0] < len(parts.values):
                value, given = parts.values[at[0]]
                at[0] += 1
                child = (id(value), id(given))
                if child in self.answers:
                    continue
                if child not in order:
                    enter(value, given)
                else:
                    # Entered and not answered: it is in the group of an expression the walk is still in.
                    low[key] = min(low[key], order[child])
                continue
            frames.pop()
            if frames:
                parent = frames[-1][0]
                low[parent] = min(low[parent], low[key])
            if low[key] == order[key]:
                self._settle(entered[places[key] :], made)
                del entered[places[key] :]

    def _settle(self, members: list[Key], made: dict[Key, Made]) -> None:
        """Answer one strongly connected group of called expressions, every value outside it already answered."""
        group = Group(members)
        inside = set(members)
        parts = {member: made[member][1] for member in members}
        values = {member: [(id(value), id(given)) for value, given in parts[member].values] for member in members}
        # Each member's values inside the group, read backwards: the members whose answer reads each member's.
        readers: dict[Key, list[Key]] = {member: [] for member in members}
        outside: dict[Key, Answer] = {}
        for member in members:
            node, member_parts, scope = made[member]
            if member_parts.callers:
                self.groups_asking.setdefault(id(scope), []).append(group)
                if len(members) == 1:
                    group.parameter = (node, scope, len(member_parts.values))
            for child in values[member]:
                if child in inside:
                    readers[child].append(member)
                elif child not in outside:
                    outside[child] = self.answers[child]
                    self._reads(group, outside[child].group)
        # Whether every value the group leads to can be told: the members reach each other, so it is one answer.
        complete = all(
            parts[member].known and all(child in inside or outside[child].complete for child in values[member])
            for member in members
        )
        told = _told(parts, values, readers, outside)
        functions = _functions(parts, values, readers, outside, told)
        for member in members:
            self.answers[member] = Answer(group, functions[member], told[member], complete)

    @staticmethod
    def _reads(group: Group, read: Group) -> None:
        """Record that a group's answers read another group's."""
        if id(read) not in group.reads:
            group.reads[id(read)] = read
            read.readers.append(group)

    def _grow(self, group: Group, answer: Answer, late: set[int]) -> None:
        """Answer again a dropped group of one parameter whose scope gained callers and whose other values kept their
        answers: its answer grows by the values the new callers give, as a parameter is told when any of its values
        is, and its functions are theirs, so it is the answer a walk from it gives anew. A new value that leads back to
        it answers it anew in the group they make together."""
        assert group.parameter is not None
        node, scope, seen = group.parameter
        key = (id(node), id(scope))
        if key in self.answers or id(scope) not in late or group.read_dropped:
            return
        callers = self.callers_of(scope)
        argument = scope.argument(node.id) if isinstance(node, ast.Name) else None
        assert argument is not None
        added = [(id(argument), id(caller)) for caller in callers[seen:]]
        for caller in callers[seen:]:
            if (id(argument), id(caller)) not in self.answers:
                self._answer(argument, caller)
            if key in self.answers:
                return
        # The group is answered again as it was, its readers and the reads that asked it dropped and run again.
        group.dropped = False
        group.readers = []
        group.askings = {}
        group.parameter = (node, scope, len(callers))
        self.groups_asking.setdefault(id(scope), []).append(group)
        for child in added:
            self._reads(group, self.answers[child].group)
            answer.functions.update(self.answers[child].functions)
        self.answers[key] = Answer(
            group,
            answer.functions,
            answer.told or any(self.answers[child].told for child in added),
            answer.complete and all(self.answers[child].complete for child in added),
        )

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
        asking = self.parameters_read[key] = Asking(self._read_parameter, scope, element, name=name)
        self.parameter_askings.setdefault(id(scope), []).append(asking)
        self._read_parameter(asking)

    def _read_parameter(self, asking: Asking) -> None:
        """Read a parameter's expression in each scope that called its scope since this read last ran."""
        callers = self.callers_of(asking.scope)
        argument = asking.scope.argument(asking.name)
        for caller in callers[asking.seen :]:
            self.value(argument, caller, asking.element)
        asking.seen = len(callers)

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
        elif element is None and (codes := self.code(node, scope)) is not None:
            self.reading.codes |= codes
        elif isinstance(node, ast.IfExp):
            self.value(node.body, scope, element)
            self.value(node.orelse, scope, element)
        elif isinstance(node, ast.Name) and scope.argument(node.id) is not None:
            self.later(self.parameter, scope, node.id, element)
        elif isinstance(node, ast.Name) and scope.function is not None and self.bound(scope.function, node.id):
            self.later(self.assigned, node, scope, element)
        elif isinstance(node, ast.Call):
            key = (id(node), id(scope), element)
            asking = self.call_askings.get(key)
            if asking is None:
                asking = self.call_askings[key] = Asking(self._read_call, scope, element, node)
            self._read_call(asking)
        elif isinstance(node, ast.Attribute) and node.attr in scope.module.properties:
            for method in scope.module.properties[node.attr]:
                self.later(self.returns, scope.module, method, self.scope(scope.module, method, (), scope), element)
        elif element is None and isinstance(node, ast.Attribute) and node.attr == ERROR_CODE:
            self.reads_error_code = True
        else:
            self._unread(node, scope)

    def code(self, node: ast.expr | None, scope: Scope) -> frozenset[int] | None:
        """The codes an expression is as it stands: None returned, which exits 0, a number, or a name bound to
        numbers."""
        if node is None or (isinstance(node, ast.Constant) and node.value is None):
            return frozenset({0})
        return self.code_of(scope.module, node)

    def assigned(self, node: ast.Name, scope: Scope, element: int | None) -> None:
        """Add the codes each value the function binds to a local name can be.

        Each value is read once for each element and each scope, as returns reads a function: a value that
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
        column = getattr(node, "col_offset", 0)
        if found not in self.unread_seen:
            self.unread_seen[found] = column
            self.reading.unread.append(found)
        else:
            self.unread_seen[found] = min(self.unread_seen[found], column)

    def exits(self, call: ast.Call, function: str, scope: Scope) -> None:
        """Add the codes an exit call or a raised SystemExit, `function` by its dotted name, exits with: no argument or
        None exits 0, a string is printed and exits 1, and a call Python refuses for its arguments, such as
        `SystemExit(code=3)`, exits 1, as exit_calls.call_code reads them, and any other argument is read as a returned
        expression is, so one the reader cannot follow is recorded as unread."""
        if (code := call_code(call, function)) is not None:
            self.reading.codes.add(code)
        else:
            self.value(exit_argument(call, function), scope)

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
                    if keyword.arg == ERROR_CODE and (numbers := self.error_code(module, keyword.value)) is not None:
                        self.error_codes |= numbers
                name = qualified_name(node.func, module.aliases)
                if name in EXIT_CALLS:
                    self.exits(node, name, self.scope(module, function, (), None))
                elif isinstance(node.func, ast.Attribute) and (
                    node.func.attr in PARSING_CALLS
                    or (node.func.attr == USAGE_ERROR_CALL and module.parsers.is_parser(node.func.value, function))
                ):
                    self.reading.codes.add(USAGE)
                    self.parsing.add(module.path)
                if isinstance(node.func, (ast.Name, ast.Attribute)) and (
                    named := self.function_named(module, node.func)
                ):
                    stack.append(named)

    def read(self, entry: Module) -> None:
        """Read what the entry point's run_main runs, a pass at a time, until a pass ends with no late caller.

        Each read waits its turn in `pending` and runs from there, so however deep the calls run, no read nests inside
        another on Python's stack, only an expression's parts, which the parser bounds. The reader reads each function,
        local, and parameter once for each element and scope, whatever pass reaches it. A caller that reaches a scope
        after a read asked the scope's callers may give that read a value it missed, so the pass is late, and the next
        runs again each read that asked a late scope's callers, directly or through an answer callables gave, each on
        what it has not read: a parameter in the callers recorded since, a call through the functions it has not
        followed. Each late pass records a caller no pass had, and the callers are bounded by the scopes, so the passes
        end; and a pass reads only what its late callers reach, so a chain of k relays, each a function handed on to
        the next through one call, takes k passes, each reading one relay's calls. A stated limit: where a function
        handed on also recurses, its call's parameter reads that parameter's grown answer and is answered again over
        every caller each pass, so k relays take time quadratic in k (1,500: about 2.5 s).

        The codes, the unread returns, and the scopes are those of a reader that read every scope in every caller the
        reading records, which is what a reader that read the whole entry point again each pass found, except as
        callables' stated limit says: where a call is not complete, its group's answer may name a function that
        reader's walk did not, or miss one, and so a code. Every read is monotone in the callers, as callables states,
        a read is run again whenever a scope it asked the callers of gains one, and so the last run of each read has
        the callers every pass reached, and its earlier runs found nothing it does not."""
        scope = self.module_scope(entry)
        self.passes = 1
        for block in main_blocks(entry.tree):
            for target in runner_targets(block, entry.aliases):
                if isinstance(target, ast.Call):
                    self.value(target, scope)
                else:
                    self._read_target(Asking(self._read_target, scope, node=target))
        self._drain()
        while self.late:
            self.passes += 1
            late, self.late = self.late, set()
            for asking in self.again(late):
                asking.run(asking)
            self._drain()

    def _drain(self) -> None:
        while self.pending:
            self.pending.pop()()

    def again(self, late: set[int]) -> list[Asking]:
        """The reads to run again for callers that reached the late scopes after a read asked their callers: each
        parameter read of a late scope, and each read that took an answer callables gave from a group that asked a late
        scope's callers or read such a group's answer, each such group dropped with its answers, and answered again
        when a read asks, or at once when it is a parameter that only gained callers: see _grow."""
        askings: dict[int, Asking] = {}
        dropped: dict[int, tuple[Group, Answer]] = {}
        groups = [group for each in late for group in self.groups_asking.pop(each, [])]
        while groups:
            group = groups.pop()
            if group.dropped:
                continue
            group.dropped = True
            for member in group.members:
                dropped[id(group)] = (group, self.answers.pop(member))
            askings.update(group.askings)
            for reader in group.readers:
                reader.read_dropped = True
            groups += group.readers
        for group, answer in dropped.values():
            if group.parameter is not None:
                self._grow(group, answer, late)
        for each in late:
            askings.update((id(asking), asking) for asking in self.parameter_askings.get(each, []))
        return list(askings.values())

    def _read_call(self, asking: Asking) -> None:
        """Follow each function a call can run that this read has not followed, and record the call as unread when the
        reader cannot tell them all."""
        call = asking.node
        assert isinstance(call, ast.Call)
        functions, complete = self.callables(call.func, asking.scope, asking)
        for module, function in functions:
            if id(function) not in asking.followed:
                asking.followed.add(id(function))
                self.later(
                    self.returns,
                    module,
                    function,
                    self.call_scope(call, module, function, asking.scope, asking.element),
                    asking.element,
                )
        if not complete:
            self._unread(call, asking.scope)

    def _read_target(self, asking: Asking) -> None:
        """Follow each function a run_main target that is no call can be that this read has not followed, given
        nothing, and read the target as a returned expression when the reader cannot tell them all."""
        target = asking.node
        assert target is not None
        functions, complete = self.callables(target, asking.scope, asking)
        if not complete:
            self.value(target, asking.scope)
        for each, function in functions:
            if id(function) not in asking.followed:
                asking.followed.add(id(function))
                self.later(self.returns, each, function, self.scope(each, function, (), asking.scope))


def runner_targets(block: ast.If, aliases: dict[str, str]) -> list[ast.expr]:
    """What each run_main call in a `__main__` block runs: its function, or the call a lambda makes."""
    targets: list[ast.expr] = []
    for node in ast.walk(block):
        if isinstance(node, ast.Call) and qualified_name(node.func, aliases) == RUNNER and node.args:
            target = node.args[0]
            targets.append(target.body if isinstance(target, ast.Lambda) else target)
    return targets


def read_codes(root: Path, entry: EntryPoint) -> CodeReader:
    """The reader of an entry point, having read the codes it can exit with and the returns it could not follow, these
    sorted by their place in the source: file, line, column, and expression. The order the reader meets them in
    depends on which pass a late caller makes read them, so a report in that order would change with the passes."""
    reader = CodeReader(root, entry.path)
    module = reader.module_at(entry.path, entry.tree)
    assert module is not None
    reader.reading.codes.add(RUNNER_FAILURE)
    reader.read(module)
    if reader.reads_error_code:
        reader.reading.codes |= reader.error_codes | reader.error_defaults()
        for owner, node in reader.error_unread:
            reader._unread(node, reader.module_scope(owner))
    reader.reading.unread.sort(key=lambda found: (found[0].as_posix(), found[1], reader.unread_seen[found], found[2]))
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

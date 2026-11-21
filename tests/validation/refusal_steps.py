"""Every deployer refusal ends with the step to take, on a line of its own, except where its module says why not
beside the code.

A refusal's lines unpacked from elsewhere are followed to where they are written (`_Refusals.ending`): a list or tuple
display, a name its function or its module binds once to one and never changes in place, through the name or a list
it hands on, or a caught refusal's own lines, and a refusal whose followed lines are one is refused like a refusal
written with one. Lines unpacked from anything else, such as a parameter, a call's result, a list grown by append, or
a list handed to another name or a call, are known only when the deployer runs, and so are a caught refusal's lines
when it may be one whose step its callers add: a cancellation, or a refusal from a function its module's STEP_ALLOWED
excuses, such as the configuration's parse. A refusal whose step could be among such lines is refused too, with its
own message, unless its module states why in STEP_ALLOWED: end it with a literal step after them, or write the step
before them, as `DeployError(error, step, *more)` does, whose lines are two at the least. A refusal raised on
unchanged, by a bare `raise` or no handler at all, is no refusal this policy reads: it is its own function's, whose
allowance's reason says which callers add the step. A caught refusal's lines are followed through the calls the `try`
body makes into the deployer's own functions, so one an excused function raises through a helper, as
`validate_directories` lets `_validate_absolute_directory`'s escape, may lack its step, and so may one from any call
whose target the policy cannot name, such as a method of a value the deployer builds at run time.

Stated limits, none of which deployer code writes:
- A deployer function or lambda handed to a builtin or an outside import, as `sorted(xs, key=check)` or
  `shutil.rmtree(p, onexc=check)`, is not followed, so a refusal it raises without its step reads as COVERED.
- A call Python makes itself, such as a property, `__enter__` or `__exit__`, `__post_init__`, an inherited
  `__init__`, or a decorator's wrapper, is not followed.
- A call through a local name, a parameter, or a module assignment that shadows a builtin or a module-level function
  is read as that builtin or function.

The deployer suites check a refusal with the harness's assert_refusal, which holds its ERROR line followed at once by
each step, and never with assert_lines, which finds each line anywhere in the output, so a step checked that way passes
wherever it drifts. An assert_lines call, as `self.assert_lines`, through any other attribute, or by a bare name an
import binds, that passes a line starting with `ERROR:` after its indentation with another line right after it, is
refused, unless its suite states why beside the code in LINES_APART_ALLOWED. Its lines are read as a refusal's are:
each positional argument, the elements of an unpacked list or tuple, written or held in a name bound once to one, and
the text a string, an f-string, a literal's `.format`, `%`, or `+`, or a name bound once to one of them starts with;
lines unpacked from anything else count as a line after an ERROR line.

Stated limits of that policy, none of which the deployer suites write:
- A call through a name bound otherwise than by an import, such as `check = self.assert_lines`, is not read.
- An ERROR line this policy cannot read as text, such as a parameter, a name bound more than once or by unpacking, a
  call's result, or an f-string that starts with a replacement field, is not read as one, and an ERROR line among
  lines unpacked from anything but a display is not found.
- An ERROR line and its step checked by separate calls, or by assertIn, are not read together.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import file_presence
from validation_support import (
    BUILTIN_NAMES,
    REPOSITORY_ROOT,
    import_aliases,
    module_allowance,
    module_statements,
    python_trees,
    qualified_name,
    relative_import_aliases,
    scoped_nodes,
    statement_scope,
)

# The deployer's refusal. A class the deployer derives from it is a refusal too, found by its bases.
REFUSAL = "deployer.errors.DeployError"
# The file that defines it, which the policy must have read for any finding, or none, to mean anything.
REFUSAL_MODULE = "deployer/errors.py"
# A refusal whose one line is the whole report, written out here rather than read from the deployer, so a change there
# cannot also change what this policy looks for: a cancellation is the user's own act, which needs no step.
STEPLESS = {"deployer.errors.Cancelled": "a cancellation is the user's own act, so it asks nothing of them"}
# A module states why a refusal ends without a step beside the code, as a module-level STEP_ALLOWED = {scope: reason},
# where a scope is a function's qualified name, such as "Journal.append", or a module-level assignment's target: a
# refusal its every caller adds the step to, or one caught beside it that never reaches the user.
STEP_ALLOWANCE = "STEP_ALLOWED"


def _scanned_files(root: Path) -> list[Path]:
    """The deployer's code: deploy.py and every module of the deployer package."""
    files = [root / "deploy.py", *file_presence.matching(root / "deployer", "**/*.py")]
    return [path for path in files if file_presence.is_present(path, other_is_absent=True)]


def _aliases(tree: ast.Module, name: str) -> dict[str, str]:
    """What each name a module binds by import stands for, its relative imports included."""
    package = ".".join(Path(name).parent.parts)
    return {**import_aliases(tree), **relative_import_aliases(tree, package)}


def _module_name(name: str) -> str:
    """A file's dotted module name, such as deployer.errors for deployer/errors.py."""
    return ".".join(Path(name).with_suffix("").parts)


def refusal_classes(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """DeployError and every class the scanned modules derive from it, directly or through another, by dotted name."""
    bases: dict[str, set[str]] = {}
    for name, tree in trees:
        aliases = _aliases(tree, name)
        module = _module_name(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found = {qualified_name(base, aliases) or "" for base in node.bases}
                # A base named in its own module, such as DeployError in deployer/errors.py, stands for that module's.
                bases[f"{module}.{node.name}"] = {base if "." in base else f"{module}.{base}" for base in found if base}
    refusals = {REFUSAL}
    grown = True
    while grown:
        derived = {name for name, parents in bases.items() if parents & refusals}
        grown = not derived <= refusals
        refusals |= derived
    return refusals


def _defines_refusal(trees: list[tuple[str, ast.Module]]) -> bool:
    """Whether the module REFUSAL names was read and defines the class at its top level."""
    module, _, class_name = REFUSAL.rpartition(".")
    return any(
        _module_name(name) == module
        and any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body)
        for name, tree in trees
    )


# How a refusal's lines end, as _Refusals.ending reads them.
STEP, ONE_LINE, COVERED, UNFOLLOWED = "step", "one line", "covered", "unfollowed"
# A list method that changes the list in place, so its display no longer shows its last line.
LIST_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"})
# The definitions whose bodies an `except` clause around them does not reach.
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# What a read of a list's name may be without handing the list on to a name or a call that could change it: the value
# a `*` unpacks, a subscript or an attribute read on it, a comparison, or what a loop iterates over.
# The scopes that hold a name's bindings.
HOLDERS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module)
LIST_READS = (ast.Starred, ast.Subscript, ast.Attribute, ast.Compare, ast.For, ast.AsyncFor, ast.comprehension)


def excused_callees(trees: list[tuple[str, ast.Module]]) -> frozenset[str]:
    """Each function a module's STEP_ALLOWED excuses, by its dotted name, such as deployer.config.parse: its refusals
    end without a step for its callers to add one."""
    return frozenset(
        f"{_module_name(name)}.{scope}"
        for name, tree in trees
        for scope in module_allowance(tree, STEP_ALLOWANCE) or {}
    )


Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class _Program:
    """The deployer's code as the policy follows a caught refusal through it: each module's name, each function and
    method by its dotted name with the reader of its module, and each class by its dotted name."""

    modules: set[str] = field(default_factory=set)
    functions: dict[str, tuple[_Refusals, Function]] = field(default_factory=dict)
    classes: set[str] = field(default_factory=set)

    def holds(self, name: str) -> bool:
        """Whether a dotted name is in the deployer's own code: one of its modules or something one defines."""
        return any(name == module or name.startswith(f"{module}.") for module in self.modules)


def _own_nodes(statements: list[ast.stmt]) -> list[ast.AST]:
    """Every node the statements run, never the body of a function, lambda, or class they define."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(statements)
    while stack:
        node = stack.pop()
        found.append(node)
        if not isinstance(node, DEFINITIONS):
            stack.extend(ast.iter_child_nodes(node))
    return found


class _Refusals:
    """How one module's refusal calls are read: which class a name stands for, and where an unpacked argument's lines
    come from."""

    def __init__(
        self,
        tree: ast.Module,
        name: str,
        refusals: set[str],
        excused: frozenset[str] = frozenset(),
        program: _Program | None = None,
    ) -> None:
        self.aliases = _aliases(tree, name)
        self.module = _module_name(name)
        self.refusals = refusals
        self.excused = excused
        self.program = program or _Program()
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def called(self, node: ast.expr) -> str | None:
        """The dotted class or function a call's target names, a bare name standing for its own module's."""
        called = qualified_name(node, self.aliases)
        return f"{self.module}.{called}" if called and "." not in called else called

    def ending(self, call: ast.Call) -> str:
        """How a refusal's lines end: STEP, ONE_LINE, COVERED, or UNFOLLOWED.

        The last positional argument is the refusal's last line, and a step when another argument comes before it to
        give the ERROR line. An unpacked last argument is followed to its lines: a list or tuple display is read
        element by element, and so is a name its function binds once to such a display, or, when the function neither
        binds nor changes it, one the module binds once to one, and that nothing changes in place, and a list display's
        name the holder hands on to nothing that could: no other name, call, return, or container. `*exc.lines` in an
        `except` clause that catches only refusals, none of them a cancellation, around code that calls no function a
        STEP_ALLOWED excuses and builds no cancellation, is COVERED: that refusal was built where this policy reads it
        and ends with a step. Anything else unpacked, such as a parameter, a call's result, a list grown by append or
        handed on, or a caught refusal that may lack its step, cannot be read: the refusal is STEP when the arguments
        before it are two lines at the least, an ERROR line and a step, and UNFOLLOWED otherwise, since its lines are
        known only when the deployer runs and this policy cannot tell whether they end with a step.
        """
        arguments = list(call.args)
        followed: set[str] = set()
        while arguments and isinstance(arguments[-1], ast.Starred):
            value = arguments[-1].value
            elements = self._displayed(value, call, followed)
            if elements is not None:
                arguments = [*arguments[:-1], *elements]
            elif self._caught_lines(value, call):
                return COVERED
            else:
                written = [argument for argument in arguments[:-1] if not isinstance(argument, ast.Starred)]
                return STEP if len(written) > 1 else UNFOLLOWED
        return STEP if len(arguments) > 1 else ONE_LINE

    def _displayed(self, value: ast.expr, call: ast.AST, followed: set[str]) -> list[ast.expr] | None:
        """The elements an unpacked value holds, read from its display, or None when they cannot be read there."""
        if isinstance(value, (ast.List, ast.Tuple)):
            return list(value.elts)
        if not isinstance(value, ast.Name):
            return None
        statement = self.assignment(value, call, followed)
        if statement is None or not isinstance(statement.value, (ast.List, ast.Tuple)):
            return None
        if isinstance(statement.value, ast.List) and self._handed_on(self._enclosing(statement, HOLDERS), value.id):
            return None
        return list(statement.value.elts)

    def assignment(self, name: ast.Name, node: ast.AST, followed: set[str]) -> ast.Assign | ast.AnnAssign | None:
        """The one assignment that binds a name read at node, in the function holding it or, when that function
        neither binds nor changes it, in the module, or None when it is bound more than once or otherwise, such as a
        parameter, changed in place, or already followed."""
        if name.id in followed:
            return None
        followed.add(name.id)
        holder = self._enclosing(node, HOLDERS)
        bindings = self._bindings(holder, name.id)
        if bindings == [] and not isinstance(holder, ast.Module):
            # A name the function neither binds nor changes is the module's, such as a remedy held in a constant.
            bindings = self._bindings(self._enclosing(holder, (ast.Module,)), name.id)
        binding = bindings[0] if bindings and len(bindings) == 1 else None
        statement = self.parents.get(binding) if binding is not None else None
        return statement if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None

    def _handed_on(self, holder: ast.AST, name: str) -> bool:
        """Whether the holder reads the name anywhere but as LIST_READS reads it, handing the list it holds to another
        name or a call, either of which may change it in place, as `alias = lines; alias.pop()` or `trim(lines)` do."""
        return any(
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
            and not self._read_in_place(node)
            for node in ast.walk(holder)
        )

    def _read_in_place(self, node: ast.Name) -> bool:
        """Whether a read of a name only reads the list it holds, as LIST_READS reads it: a loop or a comprehension
        reads it only as what it iterates over."""
        parent = self.parents.get(node)
        if isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension)):
            return parent.iter is node
        if isinstance(parent, (ast.Subscript, ast.Attribute)):
            return parent.value is node
        return isinstance(parent, LIST_READS)

    @staticmethod
    def _bindings(holder: ast.AST, name: str) -> list[ast.Name] | None:
        """Each place the holder binds the name, or None when it changes the name's list in place or takes it as a
        parameter, whose lines no display shows."""
        bindings: list[ast.Name] = []
        for node in ast.walk(holder):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
                bindings.append(node)
            elif _changes_in_place(node, name):
                return None
        return bindings

    def _caught_lines(self, value: ast.expr, call: ast.Call) -> bool:
        """Whether the value is `<name>.lines` of the innermost `except` clause holding the call that binds the name,
        when that clause catches only refusals, none of them a cancellation, does not bind the name again, and guards
        code that may raise no refusal without its step (`_guards_stepless`)."""
        if not (isinstance(value, ast.Attribute) and value.attr == "lines" and isinstance(value.value, ast.Name)):
            return False
        bound = value.value.id
        node = self.parents.get(call)
        while node is not None and not isinstance(node, DEFINITIONS):
            # A bare `except:` binds no name, so only a clause with a type can bind the one the lines are read from.
            if isinstance(node, ast.ExceptHandler) and node.name == bound and node.type is not None:
                caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
                rebound = any(
                    isinstance(child, ast.Name) and child.id == bound and isinstance(child.ctx, ast.Store)
                    for statement in node.body
                    for child in ast.walk(statement)
                )
                return (
                    not rebound
                    and all(
                        self.called(expression) in self.refusals and self.called(expression) not in STEPLESS
                        for expression in caught
                    )
                    and not self._guards_stepless(node)
                )
            node = self.parents.get(node)
        return False

    def _guards_stepless(self, handler: ast.ExceptHandler) -> bool:
        """Whether the `try` body an `except` clause guards may raise a refusal that ends without its step: it reaches
        a function a STEP_ALLOWED excuses, whose callers add the step, or a cancellation, which needs none, directly or
        through the deployer's own functions, or calls what the policy cannot follow (`_call_reaches_stepless`)."""
        guarded = self.parents.get(handler)
        body = guarded.body if isinstance(guarded, (ast.Try, ast.TryStar)) else []
        return self._reaches_stepless(body, set())

    def _reaches_stepless(self, statements: list[ast.stmt], seen: set[str]) -> bool:
        """Whether the statements may raise a refusal without its step, through any call they make."""
        return any(
            isinstance(node, ast.Call) and self._call_reaches_stepless(node, seen) for node in _own_nodes(statements)
        )

    def _call_reaches_stepless(self, call: ast.Call, seen: set[str]) -> bool:
        """Whether one call may raise a refusal without its step. An excused function or a cancellation does; a
        refusal built here is read where it is built; a deployer function, or a class's __init__, is followed into,
        each once, every handler in it read as letting the refusal through; a deployer class with no __init__ of its
        own, a builtin the module does not define again, and an import from outside the deployer reach none; and any
        other call may, since its target is known only when the deployer runs: a method of a value, a name bound
        otherwise, or a deployer name the policy finds no definition of."""
        root = call.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return True
        name = self.called(call.func) or ""
        if name in self.excused or name in STEPLESS:
            return True
        if name in self.refusals:
            return False
        target = self.program.functions.get(name) or self.program.functions.get(f"{name}.__init__")
        if target is not None:
            if name in seen:
                return False
            seen.add(name)
            reader, function = target
            return reader._reaches_stepless(function.body, seen)
        if name in self.program.classes:
            return False
        if isinstance(call.func, ast.Name) and root.id in BUILTIN_NAMES and root.id not in self.aliases:
            return False
        return self.program.holds(name) or root.id not in self.aliases

    def _enclosing(self, node: ast.AST, kinds: tuple[type[ast.AST], ...]) -> ast.AST:
        """The innermost ancestor of one of the kinds; the module holds every node."""
        while not isinstance(node, kinds):
            node = self.parents[node]
        return node


def _changes_in_place(node: ast.AST, name: str) -> bool:
    """Whether the node changes the list the name holds in place: a mutating method, a subscript stored or deleted, or
    a global or nonlocal declaration that lets another scope do so."""
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == name and node.attr in LIST_MUTATORS
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Name) and node.value.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
    return isinstance(node, ast.arg) and node.arg == name


def read_program(trees: list[tuple[str, ast.Module]], refusals: set[str], excused: frozenset[str]) -> _Program:
    """The deployer's modules, functions, and classes, each function with the reader of its module: a module-level
    function, one a module-level if, try, or with block defines, and each method of a module-level class."""
    program = _Program()
    for name, tree in trees:
        reader = _Refusals(tree, name, refusals, excused, program)
        program.modules.add(reader.module)
        for statement in module_statements(tree.body):
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                program.functions[f"{reader.module}.{statement.name}"] = (reader, statement)
            elif isinstance(statement, ast.ClassDef):
                program.classes.add(f"{reader.module}.{statement.name}")
                for method in statement.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        program.functions[f"{reader.module}.{statement.name}.{method.name}"] = (reader, method)
    return program


def stepless_refusals(
    tree: ast.Module,
    name: str,
    refusals: set[str],
    excused: frozenset[str] = frozenset(),
    program: _Program | None = None,
) -> list[tuple[int, str, str, str]]:
    """Each refusal a module builds from one line, written or followed, or from lines this policy cannot follow, as
    (line, scope, class, ending), where the ending is ONE_LINE or UNFOLLOWED, ignoring its allowance; `excused` is
    each function a STEP_ALLOWED excuses, by its dotted name, whose caught refusals may lack their step, and `program`
    the deployer's code a caught refusal is followed through."""
    reader = _Refusals(tree, name, refusals, excused, program)
    statements = [
        statement
        for statement in tree.body
        if not (isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement_scope(statement) == STEP_ALLOWANCE)
    ]
    found: list[tuple[int, str, str, str]] = []
    for node, scope in scoped_nodes(statements):
        if not isinstance(node, ast.Call):
            continue
        called = reader.called(node.func)
        if called not in refusals or called in STEPLESS:
            continue
        ending = reader.ending(node)
        if ending in {ONE_LINE, UNFOLLOWED}:
            found.append((node.lineno, scope, called.rsplit(".", 1)[-1], ending))
    return found


def _finding(name: str, line: int, scope: str, called: str, ending: str) -> str:
    """The report of one refusal that ends at its ERROR line, or whose lines this policy cannot follow."""
    if ending == UNFOLLOWED:
        return (
            f"{name}:{line} {called} in {scope} unpacks lines this policy cannot follow, such as a parameter, a "
            "call's result, or a list changed in place, so its step may be missing; write a literal step after them, "
            f"or the step before them, or state why it needs none in {STEP_ALLOWANCE}"
        )
    return (
        f"{name}:{line} {called} in {scope} ends at its ERROR line; add the step to take as its "
        f"next line, or state why it needs none in {STEP_ALLOWANCE}"
    )


def refusal_step_problems(root: Path) -> list[str]:
    """Report each deployer refusal built from one line, which ends at its ERROR line, or from lines unpacked from
    where this policy cannot follow them, without stating why in its module's STEP_ALLOWED, and each allowance no
    longer needed.

    A refusal that names what is wrong and not what to do leaves its reader stuck, so each one ends with the step to
    take on a line of its own, or its module says why it need not: every caller adds the step, or it is caught beside
    it and never reaches the user.
    """
    problems: list[str] = []
    ordered = sorted(_scanned_files(root), key=lambda path: path.relative_to(root).as_posix())
    trees = [(name, tree) for _, name, tree in python_trees(ordered, root, problems)]
    if not _defines_refusal(trees):
        # Without the class no call is read as a refusal, so an empty pass would only mean nothing was looked at.
        problems.append(
            f"{REFUSAL_MODULE} defines no {REFUSAL.rsplit('.', 1)[-1]} class, so no deployer refusal could be read; "
            "the policy checked nothing"
        )
    refusals = refusal_classes(trees)
    excused = excused_callees(trees)
    program = read_program(trees, refusals, excused)
    found: list[str] = []
    for name, tree in trees:
        allowed = module_allowance(tree, STEP_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {STEP_ALLOWANCE} must map each scope to the reason its refusals need no step")
            allowed = {}
        findings = stepless_refusals(tree, name, refusals, excused, program)
        found += [
            _finding(name, line, scope, called, ending)
            for line, scope, called, ending in findings
            if scope not in allowed
        ]
        problems += [
            f"{name}: {STEP_ALLOWANCE} allows {scope}, which no longer builds a refusal from one line or from lines "
            "this policy cannot follow"
            for scope in sorted(set(allowed) - {scope for _, scope, _, _ in findings})
        ]
    return found + problems


# The deployer suites, whose assertions of a refusal's lines the second policy reads, and the harness module they
# import, which defines both assertions.
SUITE_FOLDER = ("tests", "deployer")
HARNESS_FILE = "tests/deployer/harness.py"
# The harness's assertion that finds each line anywhere in the output, and the one that holds a refusal's ERROR line
# followed at once by each of its steps, named in the finding as the form to use.
LOOSE_ASSERTION = "assert_lines"
REFUSAL_ASSERTION = "assert_refusal"
# What a refusal's first line starts with, after the indentation recovery's report gives it.
ERROR_MARK = "ERROR:"
# A suite states why it checks an ERROR line with assert_lines apart from the line after it beside the code, as a
# module-level LINES_APART_ALLOWED = {scope: reason}, where a scope is a function's qualified name, such as
# "RecoveryTests.assert_recovery_failed": an ERROR line that is no refusal of its own, such as one recovery prints
# before it goes on, whose next line is not its step.
LINES_APART_ALLOWANCE = "LINES_APART_ALLOWED"


def _suite_files(root: Path) -> list[Path]:
    """Every Python module under tests/deployer, the harness included."""
    files = file_presence.matching(root.joinpath(*SUITE_FOLDER), "**/*.py")
    return [path for path in files if file_presence.is_present(path, other_is_absent=True)]


def _is_loose_assertion(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call is the harness's assert_lines: an attribute of that name on anything, as `self.assert_lines` or
    `DeployerTestCase.assert_lines`, or a bare name an import binds to one, as `from harness import assert_lines`."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr == LOOSE_ASSERTION
    return (
        isinstance(call.func, ast.Name)
        and call.func.id in aliases
        and qualified_name(call.func, aliases).rpartition(".")[2] == LOOSE_ASSERTION
    )


class _AssertedLines:
    """How one suite's assert_lines calls are read: the lines each passes, followed through the names that hold them
    as _Refusals follows a refusal's lines."""

    def __init__(self, tree: ast.Module, name: str) -> None:
        self.reader = _Refusals(tree, name, set())

    def lines(self, arguments: list[ast.expr], followed: frozenset[str] = frozenset()) -> list[ast.expr | None]:
        """Each positional argument as the lines it passes: an unpacked list or tuple, written or held in a name bound
        once to one, gives its elements, and anything else unpacked is None, lines this policy cannot read."""
        found: list[ast.expr | None] = []
        for argument in arguments:
            if not isinstance(argument, ast.Starred):
                found.append(argument)
                continue
            seen = set(followed)
            elements = self.reader._displayed(argument.value, argument.value, seen)
            found += [None] if elements is None else self.lines(elements, frozenset(seen))
        return found

    def text(self, node: ast.expr, followed: frozenset[str] = frozenset()) -> str:
        """The literal text a line starts with: a string, an f-string up to its first replacement field, a literal's
        `.format(...)`, `%`, or `+`, or a name bound once to one of them; empty when it starts with none."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else ""
        if isinstance(node, ast.JoinedStr):
            leading = []
            for part in node.values:
                if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                    break
                leading.append(part.value)
            return "".join(leading)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return self.text(node.func.value, followed)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self.text(node.left, followed)
        if isinstance(node, ast.Name):
            seen = set(followed)
            statement = self.reader.assignment(node, node, seen)
            if statement is not None and statement.value is not None:
                return self.text(statement.value, frozenset(seen))
        return ""

    def holds_lines_apart(self, call: ast.Call) -> bool:
        """Whether the call passes an ERROR line with another line, or lines it cannot read, right after it."""
        lines = self.lines(list(call.args))
        return any(line is not None and self.text(line).lstrip().startswith(ERROR_MARK) for line in lines[:-1])


def loose_refusal_assertions(tree: ast.Module, name: str) -> list[tuple[int, str]]:
    """Each assert_lines call a suite makes with an ERROR line right before another line, as (line, scope), ignoring
    its allowance."""
    aliases = _aliases(tree, name)
    reader = _AssertedLines(tree, name)
    return [
        (node.lineno, scope)
        for node, scope in scoped_nodes(tree.body)
        if isinstance(node, ast.Call) and _is_loose_assertion(node, aliases) and reader.holds_lines_apart(node)
    ]


def _defines(tree: ast.Module, function: str) -> bool:
    """Whether a module defines a function or method of the name, at its top level or in a top-level class."""
    definitions = [*tree.body, *(node for top in tree.body if isinstance(top, ast.ClassDef) for node in top.body)]
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function for node in definitions
    )


def loose_refusal_assertion_problems(root: Path) -> list[str]:
    """Report each deployer suite's assert_lines call that passes an ERROR line and the line after it, without stating
    why in its module's LINES_APART_ALLOWED, and each allowance no longer needed.

    assert_lines finds each line anywhere in the output, so a refusal's step checked that way passes wherever it
    drifts, or under a refusal it does not belong to; assert_refusal holds it right after its ERROR line.
    """
    problems: list[str] = []
    ordered = sorted(_suite_files(root), key=lambda path: path.relative_to(root).as_posix())
    trees = [(name, tree) for _, name, tree in python_trees(ordered, root, problems)]
    if not any(name == HARNESS_FILE and _defines(tree, LOOSE_ASSERTION) for name, tree in trees):
        # Without the harness's assertion the calls this policy reads may be spelled otherwise, so an empty pass would
        # only mean nothing was looked at.
        problems.append(
            f"{HARNESS_FILE} defines no {LOOSE_ASSERTION}, so no deployer suite's assertion could be read; the policy "
            "checked nothing"
        )
    found: list[str] = []
    for name, tree in trees:
        allowed = module_allowance(tree, LINES_APART_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {LINES_APART_ALLOWANCE} must map each scope to the reason its lines are apart")
            allowed = {}
        findings = loose_refusal_assertions(tree, name)
        found += [
            f"{name}:{line} {LOOSE_ASSERTION} in {scope} checks an ERROR line apart from the line after it; check a "
            f"refusal with {REFUSAL_ASSERTION}, which holds each step right after its ERROR line, or state why the "
            f"lines are apart in {LINES_APART_ALLOWANCE}"
            for line, scope in findings
            if scope not in allowed
        ]
        problems += [
            f"{name}: {LINES_APART_ALLOWANCE} allows {scope}, which no longer checks an ERROR line apart from the "
            "line after it"
            for scope in sorted(set(allowed) - {scope for _, scope in findings})
        ]
    return found + problems


class RefusalStepPolicies(unittest.TestCase):
    def test_every_deployer_refusal_ends_with_a_step(self) -> None:
        # A refusal added without the step to take fails here, or its module says why it needs none.
        self.assertEqual([], refusal_step_problems(REPOSITORY_ROOT))

    def test_no_deployer_suite_checks_a_refusal_and_its_step_apart(self) -> None:
        # A test that checks a refusal's ERROR line and its step with assert_lines fails here, or its suite says why.
        self.assertEqual([], loose_refusal_assertion_problems(REPOSITORY_ROOT))

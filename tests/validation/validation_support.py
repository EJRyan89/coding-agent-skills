"""What every policy module shares: the repository's roots, how a regression suite and a skill script are
recognized, the reader of a module's imports, the Markdown fence and section readers, and the fixture-tree writer.

The modules of tests/validation import the deployer, so whatever imports them first puts the repository root on
sys.path: tests/run_validation.py, or a fixture suite beside them.
"""

from __future__ import annotations

import ast
import builtins
import codecs
import re
import sys
import unittest
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from markdown_sections import CODE_SPAN as CODE_SPAN
from markdown_sections import COMMONMARK_CODE_SPAN as COMMONMARK_CODE_SPAN
from markdown_sections import Fence as Fence
from markdown_sections import find_fences as find_fences
from markdown_sections import markdown_section as markdown_section
from markdown_sections import prose_code_spans as prose_code_spans
from suite_names import SUITE_EXTENSIONS as SUITE_EXTENSIONS
from suite_names import SUITE_NAME_PATTERNS as SUITE_NAME_PATTERNS
from suite_names import is_suite_name

from deployer import platform_support

# Every git call the policies and the runner make goes through `git`, tools/release_notes.py's run through
# ejryan89-skill-core's GitClient, so each reads the checkout it names whatever GIT_DIR or GIT_INDEX_FILE the shell or a
# git hook exported.
from tools.release_notes import git as git

# The paths a repository skill's script also names: it runs from the checkout without the deployer, which every module
# here imports, and validation imports no repository skill.
DUPLICATION_ALLOWED = {
    "REPOSITORY_SKILLS": (
        ".claude/skills/change-skill/scripts/change_skill.py names the repository skills' folder to find what a change "
        "touches, and cannot import this module, which needs the deployer"
    ),
    "SKILL_GUIDE": (
        ".claude/skills/change-skill/scripts/change_skill.py prints sections of the skill contract, and cannot import "
        "this module, which needs the deployer"
    ),
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SKILLS_ROOT = REPOSITORY_ROOT / "skills"
REPOSITORY_SKILLS = ".claude/skills"
# A skill's executable files are Bash, Python, and PowerShell, each run by a standard command in deployer/tools.py.
SCRIPT_INTERPRETERS = {".bash": "bash", ".sh": "bash", ".py": platform_support.PYTHON, ".ps1": "pwsh"}
EXECUTABLE_SCRIPT_EXTENSIONS = set(SCRIPT_INTERPRETERS)
# No runtime JavaScript or TypeScript needs is a deployment prerequisite and the renderer cannot validate them, and no
# analyzer or command policy here reads a PowerShell module or a Windows batch file, so a skill holds none of these.
UNSUPPORTED_SCRIPT_EXTENSIONS = {".bat", ".cjs", ".cmd", ".js", ".mjs", ".psm1", ".ts"}
SKILL_GUIDE = "docs/adding-a-skill.md"
# Where the skill contract states the rules of validation, which a problem cites for its fix.
VALIDATION_DOC = f'"Validation" in {SKILL_GUIDE}'
# The code-review bundle's operations contract, whose threat model and format tables the policies read.
OPERATIONS_CONTRACT = "docs/code-review-operations-contract.md"
# The calls that look an attribute up by its name, as getattr(os, "environ") looks up os.environ.
ATTRIBUTE_LOOKUPS = frozenset({"getattr", "builtins.getattr"})
# The calls that import a module by its name, as the module's imports resolve them.
MODULE_LOADERS = frozenset({"importlib.import_module", "__import__", "builtins.__import__"})
# What stands for a part of a dotted name a module looks up by a name it computes, such as the module in
# importlib.import_module(name).run, which no policy can settle.
COMPUTED_NAME = "?"
# The scope a node outside every function, class, and named module-level assignment is in.
MODULE_SCOPE = "<module>"
TEMPLATE_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
SHELL_FENCES = {"bash", "sh", "shell"}


class UnreadablePython(ValueError):
    """A Python file no policy reads, with why, worded to follow the file's name in a problem."""


def python_source(path: Path) -> str:
    """A Python file's text, decoded the one way every policy decodes one: UTF-8 without a byte-order mark, its line
    endings read as newlines. A file in any other encoding raises UnreadablePython, so every policy that reads it gives
    the same answer instead of one crashing, one substituting, and one stripping a mark the others refuse."""
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        raise UnreadablePython("starts with a UTF-8 byte-order mark; save it as UTF-8 without one")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnreadablePython(f"holds a byte that is not UTF-8 at offset {error.start}; save it as UTF-8") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_source(source: str, name: str) -> ast.Module:
    """The syntax tree of Python source, or UnreadablePython when it does not parse."""
    try:
        return ast.parse(source, filename=name)
    except SyntaxError as error:
        raise UnreadablePython(f"does not parse: {error.msg} at line {error.lineno}") from None


def parse_python(path: Path) -> ast.Module:
    """The syntax tree of a Python file as python_source decodes it, or UnreadablePython."""
    return parse_source(python_source(path), path.name)


def python_trees(paths: Iterable[Path], root: Path, problems: list[str]) -> Iterator[tuple[Path, str, ast.Module]]:
    """Each Python file's path, its name relative to root, and its syntax tree; a file no policy can read is reported in
    problems by its name, and the policy goes on to the next."""
    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            tree = parse_python(path)
        except UnreadablePython as error:
            problems.append(f"{name} {error}")
            continue
        yield path, name, tree


@dataclass(frozen=True)
class ModuleImport:
    """One module an import statement imports, and what it binds each name to: `import a.b as c` imports a.b and
    binds c to a.b, `import a.b` binds a to a, and `from a import b as c` imports a and binds c to a.b."""

    line: int
    module: str
    bindings: tuple[tuple[str, str], ...]


def module_imports(tree: ast.AST) -> list[ModuleImport]:
    """Every absolute import anywhere in a module, in the order ast.walk meets it. Every policy that reads what a
    module imports reads it here; a relative import names no module these policies can find, so it is left out."""
    found: list[ModuleImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                binding = (alias.asname, alias.name) if alias.asname else (top, top)
                found.append(ModuleImport(node.lineno, alias.name, (binding,)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bindings = tuple((alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names)
            found.append(ModuleImport(node.lineno, node.module, bindings))
    return found


def import_aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a module binds by import stands for, anywhere in it: `import shutil as sh` binds sh to shutil,
    `from tempfile import TemporaryFile as T` binds T to tempfile.TemporaryFile, and `import os.path` binds os."""
    return {name: target for found in module_imports(tree) for name, target in found.bindings}


def qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    """The dotted name an expression stands for through the module's imports, such as shutil.rmtree for sh.rmtree
    after `import shutil as sh`, or "" when it is not a dotted name. A name bound otherwise stands for itself."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _unpacked(keyword: ast.keyword) -> ast.Starred:
    """A `**` unpacking as an ast.Starred around the mapping it unpacks, so it reads as a `*` unpacking does."""
    return ast.copy_location(ast.Starred(value=keyword.value, ctx=ast.Load()), keyword.value)


def call_arguments(call: ast.Call, position: int | None, keywords: Iterable[str]) -> list[ast.expr]:
    """Every argument that may give a call the parameter at a position, or one named by any of keywords, in the order
    the call writes them: the positional argument there, each keyword argument by one of those names, and each
    unpacking that may hold one: a `*` at or before the position, or any `**`. An unpacking comes back as an
    ast.Starred around what it unpacks, `**` included, so every policy reads both alike, as a value it cannot read. A
    position of None reads the keywords alone, for a parameter that only a keyword gives.

    Every policy that reads a call's arguments by position or keyword reads them here or through call_argument."""
    names = set(keywords)
    found: list[ast.expr] = []
    if position is not None:
        found += [
            argument
            for index, argument in enumerate(call.args[: position + 1])
            if index == position or isinstance(argument, ast.Starred)
        ]
    for keyword in call.keywords:
        if keyword.arg is None:
            found.append(_unpacked(keyword))
        elif keyword.arg in names:
            found.append(keyword.value)
    return found


def call_argument(call: ast.Call, position: int | None, *keywords: str) -> ast.expr | None:
    """The argument that gives a call one parameter, by its position or any of its keyword names, or None when the
    call gives it none.

    A positional argument at the position with no `*` unpacking at or before it, or a keyword argument by one of the
    names, is the value, since Python refuses a call that gives one parameter twice. Otherwise an unpacking that may
    hold it is returned as call_arguments returns one, an ast.Starred, which no policy takes for a literal."""
    leading = call.args[: position + 1] if position is not None else []
    if position is not None and len(call.args) > position and not any(isinstance(a, ast.Starred) for a in leading):
        return call.args[position]
    named = next((keyword.value for keyword in call.keywords if keyword.arg in keywords), None)
    if named is not None:
        return named
    return next((found for found in call_arguments(call, position, ()) if isinstance(found, ast.Starred)), None)


def _blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The blocks of a module-level statement that also run at module level: an if's, a try's, and a with's."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.With):
        return [node.body]
    if isinstance(node, (ast.Try, ast.TryStar)):
        return [node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody]
    return []


def module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Each statement that runs at module level, including one in a module-level if, try, or with block, which binds
    its names in the module as a top-level statement does."""
    for node in body:
        yield node
        for block in _blocks(node):
            yield from module_statements(block)


def is_main_guard(test: ast.expr) -> bool:
    """Whether an if's test compares __name__ with "__main__", written either way round: the one reading of the guard
    every policy that looks for a script's or a suite's entry point shares."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = [test.left, test.comparators[0]]
    return any(isinstance(side, ast.Name) and side.id == "__name__" for side in sides) and any(
        isinstance(side, ast.Constant) and side.value == "__main__" for side in sides
    )


DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def statement_scope(node: ast.stmt) -> str:
    """The scope a module-level statement other than a definition is: the one name it assigns, or MODULE_SCOPE."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return MODULE_SCOPE


def scoped_nodes(statements: Iterable[ast.stmt]) -> Iterator[tuple[ast.AST, str]]:
    """Each node of module-level statements, in the order a depth-first walk meets it, with the scope that holds it: the
    innermost function or class, by qualified name, or else the module-level assignment, by statement_scope. A function
    or class in a module-level block is named as one at the top is, and a definition's decorators and arguments are in
    its own scope.

    Every policy that keys an allowance by a function's qualified name or an assignment's target reads scopes here."""

    def walk(node: ast.AST, scope: str) -> Iterator[tuple[ast.AST, str]]:
        yield node, scope
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                yield from walk(child, child.name if scope == MODULE_SCOPE else f"{scope}.{child.name}")
            else:
                yield from walk(child, scope)

    for statement in statements:
        yield from walk(statement, statement.name if isinstance(statement, DEFINITIONS) else statement_scope(statement))


def listed(words: Sequence[str], conjunction: str = "or") -> str:
    """Words as a sentence lists them: one alone, two joined by the conjunction, and more with a comma after each but
    the last, the conjunction before it."""
    if len(words) < 3:
        return f" {conjunction} ".join(words)
    return f"{', '.join(words[:-1])}, {conjunction} {words[-1]}"


@dataclass
class _NameScope:
    parent: _NameScope | None
    is_class: bool = False
    is_comprehension: bool = False
    bindings: dict[str, list[ast.expr | str | None]] = field(default_factory=dict)
    aliases: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    redirects: dict[str, _NameScope] = field(default_factory=dict)

    def bind(self, name: str, value: ast.expr | str | None) -> None:
        redirected = self.redirects.get(name, self)
        if redirected is not self:
            redirected.bind(name, value)
            return
        self.bindings.setdefault(name, []).append(value)
        if (isinstance(value, ast.expr) and qualified_name(value, {})) or (
            isinstance(value, str) and not value.startswith("<local>.")
        ):
            self.aliases.add(name)

    def owner(self, name: str) -> _NameScope | None:
        redirected = self.redirects.get(name, self)
        if redirected is not self:
            return redirected.owner(name)
        if name in self.bindings:
            return self
        return self.parent.owner(name) if self.parent is not None else None


def _literal_items(node: ast.expr) -> list[ast.expr | None] | None:
    """The items of a literal tuple, list, or set that unpacks nothing, or None for any other expression."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and not any(
        isinstance(item, ast.Starred) for item in node.elts
    ):
        return [*node.elts]
    return None


class _BindingReader(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, in_place_updates: bool) -> None:
        self.scope = _NameScope(None)
        self.scopes: dict[int, _NameScope] = {}
        self.values: dict[int, list[ast.expr | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self._pair(target, node.value)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                for item in _literal_items(node.iter) or [None]:
                    self._pair(node.target, item)
        self.annotations = {
            id(node.target) for node in ast.walk(tree) if isinstance(node, ast.AnnAssign) and node.value is None
        }
        # The names an augmented assignment updates, which keep their binding when it updates the object in place.
        self.updates = {
            id(node.target)
            for node in ast.walk(tree)
            if in_place_updates and isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
        }
        self._declare(tree)
        self.visit(tree)

    def _pair(self, target: ast.expr, value: ast.expr | None) -> None:
        """Record what each name in an assignment or loop target is bound to: the matching item of a literal tuple or
        list unpacked into it, or None for a name whose value is built some other way, such as a starred name."""
        if isinstance(target, ast.Name):
            self.values.setdefault(id(target), []).append(value)
            return
        if isinstance(target, ast.Starred):
            self._pair(target.value, None)
            return
        if not isinstance(target, (ast.Tuple, ast.List)):
            return
        items = _literal_items(value) if isinstance(value, (ast.Tuple, ast.List)) else None
        star = next((index for index, part in enumerate(target.elts) if isinstance(part, ast.Starred)), None)
        if items is not None and star is not None:
            tail = len(target.elts) - star - 1
            items = [*items[:star], None, *items[len(items) - tail :]] if len(items) >= star + tail else None
        if items is None or len(items) != len(target.elts):
            items = [None] * len(target.elts)
        for part, item in zip(target.elts, items, strict=True):
            self._pair(part, item)

    def _declare(self, tree: ast.AST) -> None:
        """Reserve lexical locals before visiting nested functions, including a nonlocal bound later in its parent."""
        nodes: list[ast.AST] = []

        def visit(node: ast.AST) -> None:
            nodes.append(node)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    nodes.append(child)
                elif not isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    visit(child)

        visit(tree)
        declared_elsewhere = {
            name for node in nodes if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names
        }
        names = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
        names |= {
            node.name
            for node in nodes
            if node is not tree and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        names |= {
            name
            for node in nodes
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for found in module_imports(node)
            for name, _ in found.bindings
        }
        for name in names - declared_elsewhere:
            self.scope.bindings.setdefault(name, [])

    def visit(self, node: ast.AST) -> None:
        self.scopes[id(node)] = self.scope
        super().visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in self.annotations | self.updates:
            for value in self.values.get(id(node), [None]):
                self.scope.bind(node.id, value)

    def _argument_expressions(self, arguments: ast.arguments) -> None:
        for expression in [*arguments.defaults, *arguments.kw_defaults]:
            if expression is not None:
                self.visit(expression)
        for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        for variadic in [arguments.vararg, arguments.kwarg]:
            if variadic is not None and variadic.annotation is not None:
                self.visit(variadic.annotation)

    def _parameters(self, arguments: ast.arguments) -> None:
        """Bind each parameter as a local value, or to its default when the default is a name or an attribute, as an
        assignment binds it: what a caller passes is never resolved, so a call through the parameter is read as a call
        of its default."""
        positional = [*arguments.posonlyargs, *arguments.args]
        defaults: dict[int, ast.expr | None] = {
            id(argument): default
            for argument, default in [
                *zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True),
                *zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True),
            ]
        }
        for argument in [*positional, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]:
            if argument is None:
                continue
            self.scopes[id(argument)] = self.scope
            default = defaults.get(id(argument))
            named = isinstance(default, (ast.Name, ast.Attribute))
            self.scope.bind(argument.arg, default if named else f"<local>.{argument.arg}")

    def visit_Import(self, node: ast.Import) -> None:
        for found in module_imports(node):
            for name, target in found.bindings:
                self.scope.bind(name, target)
                owner = self.scope.owner(name)
                if owner is not None:
                    owner.imports.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            target = f"{node.module}.{alias.name}" if node.level == 0 and node.module else f"<local>.{alias.name}"
            name = alias.asname or alias.name
            self.scope.bind(name, target)
            owner = self.scope.owner(name)
            if owner is not None:
                owner.imports.add(name)

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.bind(node.name, f"<local>.{node.name}")
        for child in ast.iter_child_nodes(node):
            if child not in node.body and not isinstance(child, ast.arguments):
                self.visit(child)
        parent = self.scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._argument_expressions(node.args)
        enclosing = parent
        while enclosing.is_class and enclosing.parent is not None:
            enclosing = enclosing.parent
        self.scope = _NameScope(parent if isinstance(node, ast.ClassDef) else enclosing, isinstance(node, ast.ClassDef))
        self._declare(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._parameters(node.args)
        for statement in node.body:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._argument_expressions(node.args)
        parent = self.scope
        self.scope = _NameScope(parent)
        self._parameters(node.args)
        self.visit(node.body)
        self.scope = parent

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        self.visit(node.generators[0].iter)
        parent = self.scope
        self.scope = _NameScope(parent, is_comprehension=True)
        for index, generator in enumerate(node.generators):
            self.visit(generator.target)
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
            self.visit(expression)
        self.scope = parent

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        scope = self.scope
        while scope.is_comprehension and scope.parent is not None:
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            scope.bind(node.target.id, node.value)
        self.visit(node.value)

    def visit_Global(self, node: ast.Global) -> None:
        scope = self.scope
        while scope.parent is not None:
            scope = scope.parent
        self.scope.redirects.update(dict.fromkeys(node.names, scope))

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        parent = self.scope.parent
        if parent is not None:
            for name in node.names:
                self.scope.redirects[name] = parent.owner(name) or parent

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            # A case's whole pattern captured by name binds the subject itself; any other capture binds a part of it.
            if isinstance(case.pattern, ast.MatchAs) and case.pattern.name is not None:
                self.scope.bind(case.pattern.name, node.subject)
                if case.pattern.pattern is not None:
                    self.visit(case.pattern.pattern)
            else:
                self.visit(case.pattern)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.scope.bind(node.name, None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.scope.bind(node.rest, None)
        self.generic_visit(node)


class CallableResolver:
    """Resolve import and simple-assignment aliases in their lexical scopes, never guessing after a rebinding.

    A simple assignment binds a name to a name or an attribute: an assignment or an assignment expression, an item of
    a literal tuple or list unpacked or looped over, a parameter's default, or the subject a case captures whole.
    Other value construction is not an alias, such as a call's result, an item of anything but a literal, a with
    target, or a caught exception. Once a name is an alias, any other binding makes it unresolvable.

    An augmented assignment to a name binds it again, since `name += ...` may bind a new object, unless the reader is
    given in_place_updates, for a policy whose objects an augmented assignment updates in place, as `environ |= ...`
    updates os.environ and leaves the name bound to it.
    """

    def __init__(self, tree: ast.Module, in_place_updates: bool = False) -> None:
        self.tree = tree
        self.scopes = _BindingReader(tree, in_place_updates).scopes
        self._aliases: dict[int, dict[str, str]] = {}

    def _bound_name(self, name: str, scope: _NameScope, seen: frozenset[tuple[int, str]]) -> str:
        owner = scope.owner(name)
        if owner is None:
            return name if name in vars(builtins) else ""
        key = (id(owner), name)
        values = owner.bindings[name]
        if key in seen or len(values) != 1:
            return ""
        value = values[0]
        if isinstance(value, str):
            return value
        if isinstance(value, (ast.Name, ast.Attribute)):
            return self._name(value, seen | {key})
        return f"<local>.{name}"

    def _name(self, node: ast.AST, seen: frozenset[tuple[int, str]]) -> str:
        if isinstance(node, ast.Name):
            return self._bound_name(node.id, self.scopes[id(node)], seen)
        if isinstance(node, ast.Attribute):
            base = self._name(node.value, seen)
            return f"{base}.{node.attr}" if base else ""
        return ""

    def aliases(self, node: ast.AST) -> dict[str, str]:
        scope = self.scopes[id(node)]
        if id(scope) not in self._aliases:
            names: set[str] = set()
            parent: _NameScope | None = scope
            while parent is not None:
                names.update(parent.bindings)
                parent = parent.parent
            self._aliases[id(scope)] = {name: self._bound_name(name, scope, frozenset()) for name in names}
        return self._aliases[id(scope)]

    def name(self, node: ast.AST) -> str:
        return qualified_name(node, self.aliases(node))

    def function(self, call: ast.Call) -> ast.expr:
        """The original callable expression, so a bound path method keeps its receiver and argument positions."""
        return self.original(call.func)

    def original(self, node: ast.expr) -> ast.expr:
        """The expression a name stands for, followed through each name the module binds once to a name or an
        attribute, or the expression itself."""
        seen: set[int] = set()
        while isinstance(node, ast.Name) and id(node) not in seen:
            seen.add(id(node))
            owner = self.scopes[id(node)].owner(node.id)
            values = owner.bindings[node.id] if owner is not None else []
            if len(values) != 1 or not isinstance(values[0], (ast.Name, ast.Attribute)):
                break
            node = values[0]
        return node

    def unresolved(self, call: ast.Call) -> bool:
        if self.name(call.func):
            return False
        root = call.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        owner = self.scopes[id(root)].owner(root.id)
        return (
            owner is not None
            and root.id in owner.aliases
            and (isinstance(call.func, ast.Name) or self._imported(root.id, owner, frozenset()))
        )

    def _imported(self, name: str, scope: _NameScope, seen: frozenset[tuple[int, str]]) -> bool:
        owner = scope.owner(name)
        if owner is None or (id(owner), name) in seen:
            return False
        if name in owner.imports:
            return True
        for value in owner.bindings[name]:
            while isinstance(value, ast.Attribute):
                value = value.value
            if isinstance(value, ast.Name) and self._imported(
                value.id, self.scopes[id(value)], seen | {(id(owner), name)}
            ):
                return True
        return False

    def problems(self, path: str, skipped: set[int] | None = None) -> list[str]:
        return [
            f"{path}:{node.lineno} cannot resolve callable alias {ast.unparse(node.func)}; "
            "use a single binding to a known callable"
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and (skipped is None or id(node) not in skipped) and self.unresolved(node)
        ]


def module_lookup(node: ast.expr, resolver: CallableResolver) -> str:
    """The module an expression reaches by its name rather than by an import statement, through the module's imports:
    an importlib.import_module or __import__ call, sys.modules indexed, or a name the module binds once to one of
    these, as its dotted name when that name is one string literal and COMPUTED_NAME when it is anything else; "" for
    any other expression. An __import__ of a dotted name without a fromlist returns its top-level package, as Python's
    does."""
    if isinstance(node, ast.Name):
        owner = resolver.scopes[id(node)].owner(node.id)
        values = owner.bindings[node.id] if owner is not None else []
        bound = values[0] if len(values) == 1 else None
        return module_lookup(bound, resolver) if isinstance(bound, ast.Call | ast.Subscript) else ""
    if isinstance(node, ast.Subscript) and resolver.name(node.value) == "sys.modules":
        key = node.slice
        return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else COMPUTED_NAME
    if not isinstance(node, ast.Call) or (loader := resolver.name(node.func)) not in MODULE_LOADERS:
        return ""
    name = call_argument(node, 0, "name")
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        return COMPUTED_NAME
    if loader != "importlib.import_module" and call_argument(node, 3, "fromlist") is None:
        return name.value.partition(".")[0]
    return name.value


def fence_holders(lines: list[str]) -> list[Fence | None]:
    """For each line, the fence holding it as its opener, body, or closer, or None outside every fence.

    Every Markdown policy here reads fences through ejryan89-skill-core's find_fences, the reader the deployer renders
    with.
    """
    holders: list[Fence | None] = [None] * len(lines)
    for fence in find_fences(lines):
        for index in fence.lines():
            holders[index] = fence
    return holders


def fence_body_line(holder: Fence | None, index: int) -> bool:
    """Whether the line at index is inside the fence that holds it, rather than its opener or closer."""
    return holder is not None and index not in (holder.opener, holder.closer)


def repository_files(root: Path) -> list[Path]:
    """Tracked files plus untracked ones Git does not ignore, which is what a commit could include.

    Raises ValueError when git cannot list them.
    """
    listed = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listed.returncode != 0:
        raise ValueError(f"git ls-files failed in {root}: {listed.stderr.strip()}")
    return [root / name for name in listed.stdout.split("\0") if name and (root / name).is_file()]


# ejryan89-skill-core's lookup of a sibling skill's scripts directory, which a script puts on sys.path after the line
# that puts ejryan89-skill-core there.
SIBLING_LOOKUP = "sibling_scripts"


@dataclass(frozen=True)
class ScriptsEntry:
    """A skill's scripts directory a module reaches: by name, or None when a lookup's argument is not one literal."""

    line: int
    name: str | None
    # Whether the module names the directory by a hand-written path rather than through SIBLING_LOOKUP.
    hand_written: bool


# The calls that build a path from parts, by their last name: os.path's join and pathlib's constructors, and a path's
# joinpath, whose receiver is the first part; and the conversions whose one argument is the path they stand for.
PATH_JOINS = frozenset({"join", "Path", "PurePath", "PurePosixPath", "PureWindowsPath"})
PATH_CONVERSIONS = frozenset({"str", "fspath", "Path", "PurePath", "PurePosixPath", "PureWindowsPath"})


def path_parts(
    node: ast.AST | None, names: Mapping[str, list[ast.expr]] | None = None, seen: frozenset[str] = frozenset()
) -> list[str | None]:
    """The components a path expression is built from, in order, each literal one by its text and anything else as
    None: a `/` chain, a join of parts, a conversion of one, or a string, split at each separator, so "x/scripts" is
    two components. Given the values a module assigns each name, a name assigned once is followed to its value."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return path_parts(node.left, names, seen) + path_parts(node.right, names, seen)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [part for part in re.split(r"[/\\]", node.value) if part] or [None]
    if isinstance(node, ast.Name) and names is not None and node.id not in seen and len(names.get(node.id, [])) == 1:
        return path_parts(names[node.id][0], names, seen | {node.id})
    if isinstance(node, ast.Call) and not node.keywords:
        function = ast.unparse(node.func).rsplit(".", 1)[-1]
        arguments = [part for argument in node.args for part in path_parts(argument, names, seen)]
        if function == "joinpath" and isinstance(node.func, ast.Attribute):
            return path_parts(node.func.value, names, seen) + arguments
        if (function in PATH_JOINS and len(node.args) > 1) or (function in PATH_CONVERSIONS and len(node.args) == 1):
            return arguments
    return [None]


def _hand_written_skill(node: ast.AST) -> str | None:
    """The skill whose scripts directory a path expression names by its last two components, "<skill>/scripts"."""
    parts = path_parts(node)
    if len(parts) > 1 and parts[-1] == "scripts" and parts[-2] not in {None, ".", ".."}:
        return parts[-2]
    return None


def _path_values(tree: ast.AST, aliases: dict[str, str]) -> list[tuple[int, ast.expr]]:
    """Each value a statement in the tree puts on sys.path, with the statement's line: what an insert, append, or
    extend call passes, what a slice assignment assigns, and what an augmented assignment adds."""
    values: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append", "extend"}
            and qualified_name(node.func.value, aliases) == "sys.path"
        ):
            values += [(node.lineno, argument) for argument in [*node.args, *(item.value for item in node.keywords)]]
        elif isinstance(node, (ast.Assign, ast.AugAssign)) and any(
            qualified_name(target.value if isinstance(target, ast.Subscript) else target, aliases) == "sys.path"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        ):
            values.append((node.lineno, node.value))
    return values


def name_values(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """Every value the tree assigns each name, in any scope, so a path held in a name first is followed to it."""
    found: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.value:
            found.setdefault(node.target.id, []).append(node.value)
    return found


def scripts_entries(tree: ast.AST) -> list[ScriptsEntry]:
    """Each skill scripts directory a statement of the tree puts on sys.path, by an insert, append, or extend call, a
    slice assignment, or an augmented assignment: through a `sibling_scripts("<skill>")` call, or by a hand-written
    path whose last two components are "<skill>" and "scripts", built with `/`, a join, or one string. A value held in
    a name is followed to every value the module assigns it."""
    names = name_values(tree)
    entries: set[ScriptsEntry] = set()
    for line, value in _path_values(tree, import_aliases(tree)):
        pending, seen = [value], set()
        while pending:
            expression = pending.pop()
            for part in ast.walk(expression):
                if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load) and part.id not in seen:
                    seen.add(part.id)
                    pending += names.get(part.id, [])
                elif isinstance(part, ast.Call) and ast.unparse(part.func).split(".")[-1] == SIBLING_LOOKUP:
                    argument = part.args[0] if len(part.args) == 1 and not part.keywords else None
                    name = argument.value if isinstance(argument, ast.Constant) else None
                    entries.add(ScriptsEntry(line, name if isinstance(name, str) else None, False))
                elif skill := _hand_written_skill(part):
                    entries.add(ScriptsEntry(line, skill, True))
    return sorted(entries, key=lambda entry: (entry.line, entry.name or "", entry.hand_written))


def scripts_put_on_path(tree: ast.AST) -> list[str]:
    """The skill names whose scripts directory the tree reaches, as `scripts_entries` reads them."""
    return [entry.name for entry in scripts_entries(tree) if entry.name is not None]


def is_executable_script(path: Path) -> bool:
    return path.suffix.casefold() in EXECUTABLE_SCRIPT_EXTENSIONS


def is_shell_script(path: Path) -> bool:
    return path.suffix.casefold() in {".sh", ".bash"}


def is_test_script(path: PurePath) -> bool:
    """Whether a file is a regression suite, by ejryan89-skill-core's rule for one, which the review conditions also
    read."""
    return is_suite_name(path.name)


def path_order(path: PurePath) -> tuple[str, ...]:
    """A path's place in a listing that is the same on every platform: its parts compared without regard to case, as
    Windows compares paths, so a policy reports in one order wherever it runs."""
    return tuple(part.casefold() for part in path.parts)


def relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def is_skill_file(relative_path: PurePath) -> bool:
    """Whether a path relative to the repository is a shipped skill's SKILL.md, found as deployer/source.py finds it:
    skills/<name>/SKILL.md."""
    parts = relative_path.parts
    return len(parts) == 3 and parts[0] == "skills" and parts[-1] == "SKILL.md"


def skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Each shipped skill's directory: skills/<name>, holding SKILL.md.
    skill_tree_problems reports any other directory under skills/."""
    skills = root / "skills"
    found = (path.parent for path in skills.glob("*/SKILL.md"))
    return sorted(found, key=lambda path: path.relative_to(skills).as_posix().casefold())


def skills_by_name(root: Path = REPOSITORY_ROOT) -> dict[str, Path]:
    """Each shipped skill's directory by its name: the one lookup every policy that starts from a skill's name, in
    deploy-meta or skill_deps, uses to find its files."""
    return {path.name: path for path in skill_directories(root)}


def repository_skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Each repository skill's directory under .claude/skills, which validation holds as it holds a shipped skill."""
    return sorted((path.parent for path in (root / REPOSITORY_SKILLS).glob("*/SKILL.md")), key=lambda path: path.name)


def all_skill_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Every shipped and repository skill's directory: validation finds, lays out, lints, and types their scripts/."""
    return [*skill_directories(root), *repository_skill_directories(root)]


def skill_script_directories(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """The scripts/ directory of every shipped and repository skill that has one."""
    return [skill / "scripts" for skill in all_skill_directories(root) if (skill / "scripts").is_dir()]


def write_fixture_tree(root: Path, files: Mapping[str, str]) -> None:
    """Write each relative path's literal text under root, creating its folders."""
    for relative_path, text in files.items():
        (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (root / relative_path).write_text(text, encoding="utf-8")


def module_allowance(tree: ast.Module, name: str) -> dict[str, str] | None:
    """A module's allowance, such as PLATFORM_ALLOWED, or None when it does not map each token to its reason.

    Every policy that lets a module sanction an exception beside the code it excuses reads the allowance here, so
    they agree on its shape: a module-level literal dictionary of non-empty reasons, assigned with or without an
    annotation, empty when the module has none.
    """
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
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


def docstring_ids(tree: ast.Module) -> set[int]:
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


@dataclass(frozen=True)
class PullRequest:
    """The pull request a run judges: the files tests/run_validation.py was given with --pr-body and --pr-title, each
    None when it was not, as in a local run."""

    body: Path | None = None
    title: Path | None = None

    def body_text(self) -> str | None:
        return None if self.body is None else self.body.read_text(encoding="utf-8")

    def title_text(self) -> str | None:
        return None if self.title is None else self.title.read_text(encoding="utf-8")


class PullRequestChecks(unittest.TestCase):
    """Policy checks that judge the pull request. The runner constructs each with the one it was given; one constructed
    with none judges no body and no title, as a local run without --pr-body and --pr-title does."""

    def __init__(self, methodName: str = "runTest", pull_request: PullRequest | None = None) -> None:
        super().__init__(methodName)
        self.pull_request = PullRequest() if pull_request is None else pull_request

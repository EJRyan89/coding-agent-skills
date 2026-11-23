"""Each concept read in one helper is read only through it: the hub guard's place lookups and closing references.

Outside review passes kept finding one concept implemented twice, with one copy fixed and the other left behind. The
duplication policy catches a copied definition, not a concept implemented again with other code, so this policy holds
two such concepts to their one helper.

Place lookups. PLACE_LOOKUPS names them: checkout_of, guarded_hub_of_repository, and reached_hubs in
tools/hub_guard.py, and checkout_of in tools/worktrees.py. Each raises git_client's GitError where git cannot say where
a call acts, and a place git cannot read may be a guarded hub's, so every path that reaches one treats any GitError, of
any kind, as an unknown place. The policy reads both modules and follows each call in them that reaches a lookup: a
function of the same module called by its name, a method called through self or cls in its class, or through any other
receiver when one class of the module alone defines a method by that name, and a function of the other module called
through that module's name. A call is caught where it stands in the body of a try whose first handler for a GitError,
an except naming GitError or a class GitError derives from, or a bare except, does not raise the error it caught again;
a handler that raises another error converts every kind alike and catches it. A scope with a call of a lookup, or of a
scope that lets a lookup's GitError out, that is not caught lets that GitError out itself. It fails:
- a handler that catches such a GitError and treats its kinds apart: one that reads, of the error it caught, an
  attribute that tells one GitError from another, each one a GitError holds that a RuntimeError does not, as git_client
  defines them, such as `kind` or `answered`, or that tests it with isinstance against no class GitError derives from,
  as a handler that counts only a ReadingTimeout as cut short would;
- a scope nothing calls that lets one escape: a function no function of the two modules calls, and a module's own
  statements, such as its `__main__` block, so every entry point ends a place lookup's GitError in a handler.
PLACE_LOOKUP_ALLOWED names a scope held to neither, by its module and its scope, with the reason; an entry that
sanctions nothing, one with no reason, and a lookup PLACE_LOOKUPS names that its module does not define, fail.

Closing references. ejryan89-skill-core's closing_references.py is the one reader of the issues a pull request closes,
and holds the one copy of GitHub's closing keywords, CLOSING_KEYWORDS, which this policy reads from it. In every
non-test module ruff formats but the helper, it fails:
- a string literal given as the pattern to a function of `re` that takes one, or bound once at module level to a name
  given so, that finds a closing keyword before an issue reference: for a keyword the literal spells, the pattern finds
  a match that runs from the keyword into the reference in the keyword followed by one of SAMPLE_REFERENCES, and none
  in the same text with the keyword's letters replaced;
- a string literal holding a closing keyword before the start of an issue reference, as the helper's CLOSING_START
  reads one, that is compared with `==`, `!=`, `in`, or `not in`, or given to a method of `str`;
- a call of the helper's closing_references, which reads one text, and a match of its CLOSING_REFERENCE over anything
  but the text pull_request_text joins from a title and a body, since a pull request's squash commit carries both;
- a call of pull_request_references or pull_request_text not given both a title and a body, or given a literal None for
  either.

Stated limits, each one a fixture in tests/validation/test_one_helper.py shows:
- A place lookup handed on as a value, as `map(checkout_of, paths)`, is not followed, and neither is a call from a
  module other than the two. A method reached through a receiver whose method name more than one class of the module
  defines is not followed, so that method reads as an entry point.
- A handler that reads a kind through a function it hands the error to, or tests the error's class other than by
  isinstance, as `type(error) is GitError`, is read as treating every kind alike.
- A GitError that `contextlib.suppress` catches is not read as caught, so its scope reads as letting it out.
- An error kept and raised after its handler, as reached_hubs raises its first failure once every place is looked up,
  is read as caught; reached_hubs is a lookup, so its callers are held all the same.
- A pattern built from names or formatted values, or a pattern that needs text before the keyword, as
  `^- closes #(\\d+)`, is not read, and neither is a keyword compared alone, as `word == "closes"`, since a state such
  as `"closed"` or a subcommand such as `resolve` is compared the same way.
- A module that applies the helper's own patterns, as implement-change's pull_request.py applies BARE_CLOSING_LINE to a
  body line, reads the keywords through the helper, and is not refused.
- A Bash or PowerShell script, a workflow, and a Markdown fence are not read.
"""

from __future__ import annotations

import ast
import inspect
import re
import unittest
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import closing_references
import git_client
from python_checks import formatted_modules
from validation_support import (
    REPOSITORY_ROOT,
    CallableResolver,
    parse_python,
    python_trees,
    repository_files,
    scope_problems,
    scoped_nodes,
)

POLICY = "tests/validation/one_helper.py"

# ---------------------------------------------------------------------------------------------------------------
# Place lookups

# The hub guard's place lookups, by their module's path from the repository root.
PLACE_LOOKUPS: dict[str, frozenset[str]] = {
    "tools/hub_guard.py": frozenset({"checkout_of", "guarded_hub_of_repository", "reached_hubs"}),
    "tools/worktrees.py": frozenset({"checkout_of"}),
}
# Each scope held to neither rule, by its module's path and its scope, with the reason.
PLACE_LOOKUP_ALLOWED: dict[str, dict[str, str]] = {
    "tools/hub_guard.py": {
        "hub_protected": "an edit is judged by git's answer: a place git answered with an error for may lie in a "
        "guarded hub, so it is protected, while a git that did not run or finish says nothing of the place and goes "
        "to the hook, which allows the edit with a note, as docs/parallel-sessions.md states, so that a missing or "
        "hanging git cannot block every edit",
    },
}
# The class whose every kind is an unknown place, and the names a handler catches it by: the class and each one it
# derives from, a builtin by its bare name.
GIT_ERROR = git_client.GitError
CATCHING = frozenset(
    cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"
    for cls in GIT_ERROR.__mro__
)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def error_attributes(source: Path, name: str) -> frozenset[str]:
    """The attributes that tell one error of a class from another, read from the class's own definition: each one its
    methods assign on self, and each public method or property it defines, none of which RuntimeError holds."""
    tree = parse_python(source)
    defined = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    public = {node.name for node in defined.body if isinstance(node, FUNCTIONS) and not node.name.startswith("_")}
    assigned = {
        node.attr
        for node in ast.walk(defined)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    return frozenset(public | assigned)


# The attributes that tell one GitError from another, read from git_client's own definition of the class.
KIND_ATTRIBUTES = error_attributes(Path(git_client.__file__), GIT_ERROR.__name__)
SELF_RECEIVERS = frozenset({"self", "cls"})
LOCAL = "<local>."


@dataclass(frozen=True)
class Call:
    """A call in one of the modules that reaches a function of theirs: where it stands, and what it calls."""

    node: ast.Call
    caller: tuple[str, str]
    callee: tuple[str, str]


@dataclass
class Module:
    path: str
    tree: ast.Module
    resolver: CallableResolver
    functions: set[str]
    classes: set[str]
    enclosing: dict[int, ast.AST]


def _module(path: str, tree: ast.Module) -> Module:
    functions: set[str] = set()
    classes: set[str] = set()
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, FUNCTIONS):
            functions.add(scope)
        elif isinstance(node, ast.ClassDef):
            classes.add(scope)
    enclosing = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    return Module(path, tree, CallableResolver(tree), functions, classes, enclosing)


def _enclosing(scope: str, name: str, defined: set[str]) -> str | None:
    """The function `name` names from `scope`: one nested in it or a scope above it, or one at the top."""
    parts = scope.split(".")
    for depth in range(len(parts), 0, -1):
        candidate = ".".join([*parts[:depth], name])
        if candidate in defined:
            return candidate
    return name if name in defined else None


def _method(module: Module, scope: str, receiver: ast.expr, name: str) -> str | None:
    """The method a call of `receiver.name` from `scope` reaches: through self or cls, the one its class defines;
    through any other receiver, the one class of the module that alone defines a method by that name."""
    if isinstance(receiver, ast.Name) and receiver.id in SELF_RECEIVERS:
        parts = scope.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            owner = ".".join(parts[:depth])
            if owner in module.classes and f"{owner}.{name}" in module.functions:
                return f"{owner}.{name}"
        return None
    owners = [owner for owner in module.classes if f"{owner}.{name}" in module.functions]
    return f"{owners[0]}.{name}" if len(owners) == 1 else None


def _callee(module: Module, modules: Mapping[str, Module], scope: str, call: ast.Call) -> tuple[str, str] | None:
    """The function of the modules a call reaches, by its module's path and its scope, or None."""
    name = module.resolver.name(call.func)
    stems = {PurePosixPath(path).stem: path for path in modules}
    head, _, rest = name.partition(".")
    if head in stems and rest and "." not in rest and stems[head] != module.path:
        other = modules[stems[head]]
        return (other.path, rest) if rest in other.functions else None
    if isinstance(call.func, ast.Name) and name.startswith(LOCAL):
        found = _enclosing(scope, name[len(LOCAL) :], module.functions)
        return None if found is None else (module.path, found)
    if isinstance(call.func, ast.Attribute):
        found = _method(module, scope, call.func.value, call.func.attr)
        return None if found is None else (module.path, found)
    return None


def _calls(modules: Mapping[str, Module]) -> list[Call]:
    found: list[Call] = []
    for module in modules.values():
        for node, scope in scoped_nodes(module.tree.body):
            if isinstance(node, ast.Call):
                callee = _callee(module, modules, scope, node)
                if callee is not None:
                    found.append(Call(node, (module.path, scope), callee))
    return found


def _catches(handler: ast.ExceptHandler, resolver: CallableResolver) -> bool:
    """Whether a handler catches a GitError: a bare except, or one naming GitError or a class it derives from."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(resolver.name(each) in CATCHING for each in types)


def _handler_nodes(handler: ast.ExceptHandler) -> Iterator[ast.AST]:
    """The nodes of a handler's body, past any function or class it defines, whose body runs elsewhere."""
    pending: list[ast.AST] = list(handler.body)
    while pending:
        node = pending.pop()
        yield node
        pending += [child for child in ast.iter_child_nodes(node) if not isinstance(child, (*FUNCTIONS, ast.Lambda))]


def raises_again(handler: ast.ExceptHandler) -> bool:
    """Whether a handler raises the error it caught again, by a bare raise or by its name."""
    return any(
        isinstance(node, ast.Raise)
        and (node.exc is None or (isinstance(node.exc, ast.Name) and node.exc.id == handler.name))
        for node in _handler_nodes(handler)
    )


def kinds_apart(handler: ast.ExceptHandler, resolver: CallableResolver) -> list[str]:
    """How a handler that catches a GitError tells one kind from another: each attribute of KIND_ATTRIBUTES it reads of
    the error it caught, and each isinstance test of it against no class GitError derives from."""
    if handler.name is None:
        return []
    found: list[str] = []
    for node in _handler_nodes(handler):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == handler.name
            and node.attr in KIND_ATTRIBUTES
        ):
            found.append(f"reads {handler.name}.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and resolver.name(node.func) == "isinstance"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == handler.name
        ):
            tested = node.args[1].elts if isinstance(node.args[1], ast.Tuple) else [node.args[1]]
            names = [resolver.name(each) for each in tested]
            if not CATCHING & set(names):
                found.append(f"tests {handler.name} with isinstance against {', '.join(names) or 'a computed class'}")
    return sorted(set(found))


def catching_handler(call: ast.Call, module: Module) -> ast.ExceptHandler | None:
    """The handler that catches a GitError the call raises within its own scope: the first that catches one of the
    innermost try whose body holds the call and that has one; None where none does."""
    node: ast.AST = call
    while id(node) in module.enclosing:
        holder = module.enclosing[id(node)]
        if isinstance(holder, (*FUNCTIONS, ast.Lambda, ast.ClassDef)):
            return None
        if isinstance(holder, (ast.Try, ast.TryStar)) and node in holder.body:
            handler = next((each for each in holder.handlers if _catches(each, module.resolver)), None)
            if handler is not None:
                return handler
        node = holder
    return None


def place_lookup_problems(
    root: Path,
    lookups: Mapping[str, frozenset[str]] = PLACE_LOOKUPS,
    allowed: Mapping[str, Mapping[str, str]] = PLACE_LOOKUP_ALLOWED,
) -> list[str]:
    """Report each handler on a path to a place lookup that treats a GitError's kinds apart, each scope nothing calls
    that lets a place lookup's GitError escape, each lookup its module does not define, and each entry of `allowed` that
    sanctions nothing or gives no reason."""
    problems: list[str] = []
    modules = {
        name: _module(name, tree) for _, name, tree in python_trees([root / path for path in lookups], root, problems)
    }
    problems += [
        f"{POLICY}: PLACE_LOOKUPS names {path} {name}, which its module does not define; remove it or name the lookup "
        "by its scope"
        for path, names in sorted(lookups.items())
        for name in sorted(names)
        if path in modules and name not in modules[path].functions
    ]
    calls = _calls(modules)
    reaching = {(path, name) for path, names in lookups.items() for name in names}
    caught = {id(call.node): catching_handler(call.node, modules[call.caller[0]]) for call in calls}
    changed = True
    while changed:
        changed = False
        for call in calls:
            handler = caught[id(call.node)]
            if call.callee in reaching and call.caller not in reaching and (handler is None or raises_again(handler)):
                reaching.add(call.caller)
                changed = True
    found: dict[tuple[str, str], str] = {}
    for call in calls:
        handler = caught[id(call.node)]
        if call.callee not in reaching or handler is None or call.caller in found:
            continue
        apart = kinds_apart(handler, modules[call.caller[0]].resolver)
        if apart:
            found[call.caller] = (
                f"{call.caller[0]}:{handler.lineno} {call.caller[1]} catches the GitError of a place lookup that "
                f"{call.callee[1]} raises and treats its kinds apart ({'; '.join(apart)}); treat every GitError as an "
                f"unknown place, or name the scope with its reason in {POLICY}'s PLACE_LOOKUP_ALLOWED"
            )
    called = {call.callee for call in calls}
    for call in sorted(calls, key=lambda each: (each.caller, each.node.lineno)):
        caller = call.caller
        handler = caught[id(call.node)]
        entry = caller[1] not in modules[caller[0]].functions or caller not in called
        escapes = call.callee in reaching and (handler is None or raises_again(handler))
        if entry and escapes and caller not in found:
            found[caller] = (
                f"{caller[0]}:{call.node.lineno} {caller[1]}, which nothing in {' or '.join(sorted(modules))} calls, "
                f"lets the GitError of a place lookup that {call.callee[1]} raises escape; catch it there and treat "
                f"every kind as an unknown place, or name the scope with its reason in {POLICY}'s PLACE_LOOKUP_ALLOWED"
            )
    problems += [problem for key, problem in sorted(found.items()) if key[1] not in allowed.get(key[0], {})]
    problems += [
        f"{POLICY}: PLACE_LOOKUP_ALLOWED names {path} {scope}, which neither treats a place lookup's GitError kinds "
        "apart nor lets one escape; remove the entry"
        if (path, scope) not in found
        else f"{POLICY}: PLACE_LOOKUP_ALLOWED gives {path} {scope} no reason; state why it is held to neither rule"
        for path, scopes in sorted(allowed.items())
        for scope, reason in sorted(scopes.items())
        if (path, scope) not in found or not reason.strip()
    ]
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Closing references

HELPER = "skills/ejryan89-skill-core/scripts/closing_references.py"
HELPER_MODULE = closing_references.__name__
# A closing keyword standing as a word, read with the helper's own table: after no letter or digit, or after a pattern's
# escape such as `\b`, and before none.
KEYWORD = re.compile(
    rf"(?:(?<![A-Za-z0-9])|(?<=\\[A-Za-z])){closing_references.CLOSING_KEYWORDS}(?![A-Za-z0-9])", re.IGNORECASE
)
# A reference in each form the helper reads, which a closing keyword before it closes.
SAMPLE_REFERENCES = ("#1", "o/r#1", "GH-1", "https://github.com/o/r/issues/1")


def pattern_functions(source: Path, exported: Iterable[str]) -> frozenset[str]:
    """The functions of a module, by name, that take a pattern first, read from its source: each one it exports whose
    first parameter is named pattern, and each exported name it binds at its top level to one, as re binds `match` to
    `prefixmatch` where it renamed it."""
    body = parse_python(source).body
    names = set(exported)
    defined = {
        node.name
        for node in body
        if isinstance(node, ast.FunctionDef) and [argument.arg for argument in node.args.args[:1]] == ["pattern"]
    }
    bound = {
        target.id
        for node in body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in defined
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return frozenset(name for name in defined | bound if name in names)


# The functions of re that take a pattern first, read from re's own source.
PATTERN_FUNCTIONS = frozenset(f"{re.__name__}.{name}" for name in pattern_functions(Path(re.__file__), re.__all__))
COMPARISONS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
STR_METHODS = frozenset(name for name in dir(str) if not name.startswith("_"))
# The helper's readers of a text's closing references, which a caller reads only over a title and a body together.
ONE_TEXT_READER = f"{HELPER_MODULE}.{closing_references.closing_references.__name__}"
# The helper's compiled reader of a closing reference, by the name it binds; the fixture suite holds that it binds one.
REFERENCE_PATTERN = f"{HELPER_MODULE}.CLOSING_REFERENCE"
JOINED_TEXT = f"{HELPER_MODULE}.{closing_references.pull_request_text.__name__}"
BOTH_READERS = frozenset({f"{HELPER_MODULE}.{closing_references.pull_request_references.__name__}", JOINED_TEXT})
BOTH_PARTS = tuple(inspect.signature(closing_references.pull_request_text).parameters)
READ_ONE_TEXT = (
    "read the issues a pull request closes from its title and its body together, through pull_request_references, "
    f"as the squash commit carries both; see {POLICY}"
)


def finds_closing_reference(pattern: str) -> bool:
    """Whether a pattern finds a closing keyword it spells before an issue reference: a match that runs from the
    keyword into the reference, in the keyword followed by a sample reference, and none in the same text with the
    keyword's letters replaced. A pattern that does not compile finds none."""
    try:
        compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError):
        return False

    def spans_reference(text: str, length: int) -> bool:
        return any(match.start() < length and match.end() > length + 1 for match in compiled.finditer(text))

    for keyword in {match.group() for match in KEYWORD.finditer(pattern)}:
        for reference in SAMPLE_REFERENCES:
            if spans_reference(f"{keyword} {reference}", len(keyword)) and not spans_reference(
                f"{'x' * len(keyword)} {reference}", len(keyword)
            ):
                return True
    return False


def _module_strings(tree: ast.Module) -> dict[str, ast.Constant]:
    """Each name a module binds once at its top level to a string literal, with the literal."""
    bound: dict[str, list[ast.expr]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(statement.value)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
            bound.setdefault(statement.target.id, []).append(statement.value)
    return {
        name: values[0]
        for name, values in bound.items()
        if len(values) == 1 and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str)
    }


def _string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _compared(node: ast.AST) -> list[ast.expr]:
    """The literals a comparison or a call of a str method reads text against."""
    if isinstance(node, ast.Compare) and any(isinstance(op, COMPARISONS) for op in node.ops):
        operands = [node.left, *node.comparators]
        return [
            item
            for operand in operands
            for item in (operand.elts if isinstance(operand, ast.Tuple | ast.List | ast.Set) else [operand])
        ]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in STR_METHODS:
        return list(node.args)
    return []


def _given_both(call: ast.Call) -> bool:
    """Whether a call is given a title and a body, each once, by position or by name, neither a literal None."""
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return False
    named = [keyword.arg for keyword in call.keywords]
    filled = [*BOTH_PARTS[: len(call.args)], *named]
    given = [*call.args, *(keyword.value for keyword in call.keywords)]
    return sorted(filled, key=str) == sorted(BOTH_PARTS) and not any(
        isinstance(argument, ast.Constant) and argument.value is None for argument in given
    )


def module_closing_problems(name: str, tree: ast.Module) -> list[str]:
    """Each place a module reads a closing reference other than through the helper, as its line and its refusal."""
    resolver = CallableResolver(tree)
    strings = _module_strings(tree)
    found: list[tuple[int, str]] = []
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.Call):
            called = resolver.name(node.func)
            if called in PATTERN_FUNCTIONS and node.args:
                argument = node.args[0]
                literal = strings.get(argument.id) if isinstance(argument, ast.Name) else argument
                text = None if literal is None else _string(literal)
                if text is not None and finds_closing_reference(text):
                    found.append(
                        (
                            node.lineno,
                            f"{scope} matches a closing keyword before an issue reference with its own "
                            f"pattern; read closing references through {HELPER}",
                        )
                    )
            elif called == ONE_TEXT_READER:
                found.append((node.lineno, f"{scope} reads one text's closing references; {READ_ONE_TEXT}"))
            elif called in BOTH_READERS and not _given_both(node):
                found.append((node.lineno, f"{scope} gives {called} no title or no body; {READ_ONE_TEXT}"))
            elif called.startswith(f"{REFERENCE_PATTERN}.") and not (
                node.args
                and isinstance(node.args[0], ast.Call)
                and resolver.name(node.args[0].func) == JOINED_TEXT
                and _given_both(node.args[0])
            ):
                found.append((node.lineno, f"{scope} matches CLOSING_REFERENCE over one text; {READ_ONE_TEXT}"))
        for compared in _compared(node):
            text = _string(compared)
            if text is not None and closing_references.CLOSING_START.search(text):
                found.append(
                    (
                        compared.lineno,
                        f"{scope} compares text with a closing keyword before an issue "
                        f"reference, {text!r}; read closing references through {HELPER}",
                    )
                )
    return [f"{name}:{line} {refusal}" for line, refusal in sorted(set(found))]


def closing_modules(root: Path, files: Iterable[Path]) -> list[Path]:
    """The modules the closing-reference rule reads: every non-test module ruff formats but the helper."""
    return [root / name for name in formatted_modules(root, list(files)) if name != HELPER]


def closing_reference_problems(root: Path, files: Iterable[Path] | None = None) -> list[str]:
    """Report each place a module other than the helper reads a closing reference itself, or reads one text alone."""
    listed = repository_files(root) if files is None else list(files)
    problems: list[str] = []
    for _, name, tree in python_trees(closing_modules(root, listed), root, problems):
        problems += module_closing_problems(name, tree)
    return problems


class OneHelperPolicies(unittest.TestCase):
    def test_every_path_to_a_place_lookup_treats_any_git_error_as_an_unknown_place(self) -> None:
        self.assertEqual([], place_lookup_problems(REPOSITORY_ROOT))

    def test_closing_references_are_read_only_through_their_one_helper(self) -> None:
        self.assertEqual([], closing_reference_problems(REPOSITORY_ROOT))

    def test_the_closing_reference_rule_reads_every_module_its_rule_speaks_of(self) -> None:
        listed = repository_files(REPOSITORY_ROOT)
        modules = formatted_modules(REPOSITORY_ROOT, listed)
        read = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in closing_modules(REPOSITORY_ROOT, listed)]
        self.assertEqual(
            [], scope_problems("the closing-reference rule", modules, read, {HELPER: "the one helper itself"})
        )

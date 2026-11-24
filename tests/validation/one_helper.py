"""Each concept read in one helper is read only through it: the hub guard's place lookups, closing references, a
checkout's status, a pull request's files, a repository path read from a snapshot, and an owner/name pattern.

Outside review passes kept finding one concept implemented twice, with one copy fixed and the other left behind. The
duplication policy catches a copied definition, not a concept implemented again with other code, so this policy holds
each concept below to its helper.

Place lookups. PLACE_LOOKUPS names them: checkout_of, guarded_hub_of_repository, and reached_hubs in tools/hub_guard.py,
and checkout_of in tools/worktrees.py. Each raises git_client's GitError where git cannot say where a call acts, and a
place git cannot read may be a guarded hub's, so every path that reaches one treats any GitError, of any kind, as an
unknown place. The policy reads tools/hub_guard.py and tools/worktrees.py and follows each call in them that reaches a
lookup: a function of the same module called by its name, a method called through self or cls in its class, or through
any other receiver when one class of the module alone defines a method by that name, and a function of the other module
called through that module's name. A call is caught where it stands in the body of a try whose first handler for a
GitError, an except naming GitError or a class GitError derives from, or a bare except, does not raise the error it
caught again; a handler that raises another error converts every kind alike and catches it. A scope with a call of a
lookup, or of a scope that lets a lookup's GitError out, that is not caught lets that GitError out itself. It fails:
- a handler that catches such a GitError and treats its kinds apart: one that reads, of the error it caught, an
  attribute that tells one GitError from another, each one a GitError holds that a RuntimeError does not, as git_client
  defines them, such as `kind` or `answered`, or that tests it with isinstance against no class GitError derives from,
  as a handler that counts only a ReadingTimeout as cut short would;
- a scope nothing calls that lets one escape: a function no function of those modules calls, and a module's own
  statements, such as its `__main__` block, so every entry point ends a place lookup's GitError in a handler.
PLACE_LOOKUP_SANCTIONS names a scope held to neither, by its module and its scope, with the reason; an entry that
sanctions nothing, one with no reason, a module PLACE_LOOKUPS names that is not there or does not parse, and a lookup it
names that its module does not define or no call in those modules reaches, fail, so a path the policy cannot measure
never passes.

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
  module PLACE_LOOKUPS does not name. A method reached through a receiver whose method name more than one class of the
  module defines is not followed, so that method reads as an entry point.
- A handler that reads a kind through a function it hands the error to, or through an alias of the error it caught, as
  `error = exc; error.kind`, or tests the error's class other than by isinstance, as `type(error) is GitError`, is read
  as treating every kind alike, and so is a handler that raises a new GitError from the one it caught.
- A handler that catches only a class derived from GitError is read as not catching, so the error is followed to the
  next handler, and where that is guard's `except Exception`, nothing is refused; the repository defines no such class.
- The rule reads whether a handler tells kinds apart, never what it makes of the place: `except GitError: hub = None`
  passes, and so does a lookup whose error reaches guard's `except Exception`.
- Only the lookups PLACE_LOOKUPS names are followed: a git call a scope makes itself, as own_paths and
  git_directory_holding make theirs, is not.
- A GitError that `contextlib.suppress` catches is not read as caught, so its scope reads as letting it out.
- An error kept and raised after its handler, as reached_hubs raises its first failure once every place is looked up, is
  read as caught; reached_hubs is a lookup, so its callers are held all the same.
- A pattern built from names or formatted values, joined with `+`, bound to a name inside a function, or given by
  keyword as `pattern=`, a keyword written as a character class, as `[Cc]loses`, and a pattern that needs text before
  the keyword, as `^- closes #(\\d+)`, are not read, and neither is a keyword compared alone, as `word == "closes"`,
  since a state such as `"closed"` or a subcommand such as `resolve` is compared the same way.
- A title or a body given as a literal empty string is read as given.
- A module that applies the helper's own patterns, as implement-change's pull_request.py applies BARE_CLOSING_LINE to a
  body line, reads the keywords through the helper, and is not refused.
- A Bash or PowerShell script, a workflow, and a Markdown fence are not read.

Status reads. ejryan89-skill-core's git_worktrees.py is the one reader of what `git status` lists for a checkout: its
`changes` passes `--no-optional-locks`, since a status otherwise refreshes the checkout's index and writes it back
under its lock, which a session committing in that checkout at the same moment needs. In every non-test module ruff
formats but the helper, it fails a call given git arguments whose subcommand, as git_client's `subcommand` reads it
past git's own options, is `status`: a list or a tuple given by position, or a run of strings given one at a time to a
wrapper that takes `*arguments`, as `git(services, root, "status", "--porcelain")`, that holds an option after the
subcommand. An element that is not a string literal or a name the module binds once at its top level to one is read
as a value, so `["-C", path, "status"]` is read as `status`. A module states why a scope runs status itself in a
module-level STATUS_READ_ALLOWED = {scope: reason}; an entry that sanctions nothing, or one with no reason, fails. Its
stated limits, each one a fixture in tests/validation/test_one_helper.py shows:
- Arguments bound to a name first, joined with `+`, given by keyword, or led by a starred element are not read.
- A run of strings given one at a time ends at the first argument that is not one, so `git("-C", path, "status")` is
  not read, and a run with no option after `status`, as `git("status")`, is not read, since a word given alone, as
  `entry.get("status")`, is spelled alike.
- A run of strings given one at a time to a call that is not git's, where `status` is followed by a word that starts
  with `-`, as `entry.get("status", "-")` or `parser.add_argument("status", "-s")`, is refused all the same, and its
  scope states why in STATUS_READ_ALLOWED; no repository code writes one.
- An argument list that names the program first, as `["git", "status"]`, reads `git` as its subcommand; every git call
  goes through GitClient, which names the program itself.
- A Bash or PowerShell script, a workflow, and a Markdown fence are not read; no Bash or PowerShell script in the
  repository but the suites runs `git status` without `--no-optional-locks`, and the suites' own, as the update
  fixture's self-tests of its setup helper, read throwaway repositories.

Pull request files. ejryan89-code-review-core's review_github.py is the one reader of GitHub's files endpoint of a pull
request: its GitHubClient.list_pull_files reads the pull request's count of changed files and its head before the
pages, and answers with a PullFiles whose checks, each public method or property the class defines, which this policy
reads from the class, tell a caller whether the listing is cut short of that count (`cut`) and whether the head moved
from the one the caller reads the change at (`moved_from`). In every non-test module ruff formats but the helper, it
fails:
- a string literal, or an f-string read with each formatted value as a value, that spells `pulls/<number>/files` with
  the number a literal or a value, ending at a word end, so `pulls/{n}/filesystem` is not the endpoint;
- a call of a function or a method named list_pull_files whose result is not bound to one name, as by `listing = ...`
  or an assignment expression, or whose scope does not read every one of the checks as an attribute of that name or of
  the assignment expression that binds it, as `(listing := github.list_pull_files(...)).cut`.
PULL_FILES_SANCTIONS names a scope that reads the endpoint itself, by its module and its scope, with the reason; an
entry that sanctions nothing, or one with no reason, fails. It names the shepherd's every_file, which pages the
endpoint through ejryan89-skill-core's gh client: this policy holds only that the scope still reads the endpoint, and
the shepherd's own suite, in the test SHEPHERD_FILES_TEST names, which the fixture suite holds is there, holds that it
refuses a listing short of the count and one read while the head moved. Its stated limits, each one a fixture in
tests/validation/test_one_helper.py shows:
- An endpoint joined with `+`, built by `format()`, `%`, or `str.join`, or spelled as `/files` after a value that holds
  the rest, as `f"{pull}/files"`, is not read.
- gh's own reader of a pull request's files, `--json files`, and GraphQL's `files` connection are not read.
- A check anywhere in the scope counts, before the read or in a branch that never runs, and a check made in another
  function the listing is handed to does not.
- A Bash or PowerShell script, a workflow, and a Markdown fence are not read.

Snapshot paths. ejryan89-skill-core's diff_paths.py is the one reader of a snapshot's file by its repository path: its
snapshot_path judges the name with unsafe_snapshot_name, the rule the snapshot's writer applies, and then holds the
joined path inside the resolved root, so a name that leads out through a link is refused too. In every non-test module
ruff formats but the helper, it fails a scope that gives a name to the helper's unsafe_snapshot_name, resolved through
the module's imports, and joins that same name onto a path itself: by `/`, by a `joinpath` method, or by os.path's,
posixpath's, or ntpath's `join`, the name anywhere inside an operand. A scope that judges a name without joining it,
as a manifest's reader that refuses a listed name does, passes. A module states why a scope joins one itself in a
module-level SNAPSHOT_PATH_READ_ALLOWED = {scope: reason}; an entry that sanctions nothing, or one with no reason,
fails. Its stated limits, each one a fixture in tests/validation/test_one_helper.py shows:
- A name judged in one scope and joined in another, joined under another name, or judged as anything but a name, as
  `entry.path`, is not read.
- A join by `Path(root, name)`, by `/=`, or in an f-string, and a judge given the name by keyword, are not read.
- A join with no judge at all is not read: the rule finds a second judge of a snapshot path, never a reader that judges
  nothing.

Repository names. ejryan89-skill-core's github_remote.py holds the one spelling of the characters a repository's owner
and name hold, REPOSITORY_CHARACTERS, and OWNER_AND_NAME, an owner and a name of them, which closing_references.py,
repo-cleanup's pr_status.py, and the review core's review_config.py build on; review_config's repository identity keeps
its narrower rule, a login for the owner and a name never made only of dots, on top of the class. The policy reads the
class from the helper. In every non-test module ruff formats but the helper, it fails a string literal that spells an
owner/name pattern with its own class: a character class that takes exactly the characters the helper's takes, of
every ASCII character and a letter outside it, then a quantifier, lazy or not, a `/`, escaped or not, and the same class
again, its characters in any order, either class in a group or not: plain, non-capturing, or named, as
`(?P<owner>[...]+)/(?P<name>[...]+)` with the class in each group. A class `re` cannot compile, as `[\\q]`, measures
as nothing, so it neither raises nor reads as the helper's. A module states why a scope spells one in a module-level
REPOSITORY_PATTERN_ALLOWED = {scope: reason}; an entry that sanctions nothing, or one with no reason, fails. Its
stated limits, each one a fixture in tests/validation/test_one_helper.py shows:
- Each class is measured as written, under no flags, and the flags a pattern is compiled with are not read: a class
  that takes another set of characters is another class, so `[\\w.-]`, which takes letters outside ASCII unless
  `re.ASCII` is set, and `[a-z0-9_.-]`, which takes no capital unless `re.IGNORECASE` is set, are not read under those
  flags either. Neither is a class whose body holds what `re` reads as a set operation, as `--`.
- A pattern joined with `+`, split by an f-string's formatted value, spelled with spaces around the `/` in a verbose
  pattern, or with a possessive quantifier, as `++`, and a bytes literal are not read.
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
import diff_paths
import file_presence
import git_client
import git_worktrees
import github_remote
from python_checks import formatted_modules, sanctioned_problems
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
PLACE_LOOKUP_SANCTIONS: dict[str, dict[str, str]] = {
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


def _class_definition(source: Path, name: str) -> ast.ClassDef:
    tree = parse_python(source)
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def public_members(source: Path, name: str) -> frozenset[str]:
    """Each public method or property a class defines, read from its own definition."""
    defined = _class_definition(source, name)
    return frozenset(
        node.name for node in defined.body if isinstance(node, FUNCTIONS) and not node.name.startswith("_")
    )


def error_attributes(source: Path, name: str) -> frozenset[str]:
    """The attributes that tell one error of a class from another, read from the class's own definition: each one its
    methods assign on self, and each public method or property it defines, none of which RuntimeError holds."""
    defined = _class_definition(source, name)
    public = public_members(source, name)
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
    allowed: Mapping[str, Mapping[str, str]] = PLACE_LOOKUP_SANCTIONS,
) -> list[str]:
    """Report each handler on a path to a place lookup that treats a GitError's kinds apart, each scope nothing calls
    that lets a place lookup's GitError escape, each lookup module that is not there or does not parse, each lookup its
    module does not define or no call in the modules reaches, and each entry of `allowed` that sanctions nothing or
    gives no reason. A module the policy cannot read is named, never measured as one whose paths pass."""
    problems: list[str] = []
    readable: list[Path] = []
    for path in sorted(lookups):
        reason = file_presence.refusal_reason(root / path)
        if reason is None:
            readable.append(root / path)
        else:
            problems.append(
                f"{POLICY}: PLACE_LOOKUPS names {path}, which {reason}, so no path to its lookups can be measured"
            )
    modules = {name: _module(name, tree) for _, name, tree in python_trees(readable, root, problems)}
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
                f"unknown place, or name the scope with its reason in {POLICY}'s PLACE_LOOKUP_SANCTIONS"
            )
    called = {call.callee for call in calls}
    problems += [
        f"{POLICY}: PLACE_LOOKUPS names {path} {name}, which no call in {' or '.join(sorted(modules))} reaches, so no "
        "path to it is measured; remove it, or name the lookup its callers call"
        for path, names in sorted(lookups.items())
        for name in sorted(names)
        if path in modules and name in modules[path].functions and (path, name) not in called
    ]
    for call in sorted(calls, key=lambda each: (each.caller, each.node.lineno)):
        caller = call.caller
        handler = caught[id(call.node)]
        entry = caller[1] not in modules[caller[0]].functions or caller not in called
        escapes = call.callee in reaching and (handler is None or raises_again(handler))
        if entry and escapes and caller not in found:
            found[caller] = (
                f"{caller[0]}:{call.node.lineno} {caller[1]}, which nothing in {' or '.join(sorted(modules))} calls, "
                f"lets the GitError of a place lookup that {call.callee[1]} raises escape; catch it there and treat "
                "every kind as an unknown place, or name the scope with its reason in "
                f"{POLICY}'s PLACE_LOOKUP_SANCTIONS"
            )
    problems += [problem for key, problem in sorted(found.items()) if key[1] not in allowed.get(key[0], {})]
    problems += [
        f"{POLICY}: PLACE_LOOKUP_SANCTIONS names {path} {scope}, which neither treats a place lookup's GitError kinds "
        "apart nor lets one escape; remove the entry"
        if (path, scope) not in found
        else f"{POLICY}: PLACE_LOOKUP_SANCTIONS gives {path} {scope} no reason; state why it is held to neither rule"
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


# ---------------------------------------------------------------------------------------------------------------
# Status reads

STATUS_HELPER = "skills/ejryan89-skill-core/scripts/git_worktrees.py"
# The subcommand only the helper runs, read from the helper's own arguments.
STATUS = git_client.subcommand(git_worktrees.STATUS_ARGUMENTS)
# An element of an argument list that is not a string the module spells: a value, never an option or a subcommand
# git would read as `status`.
VALUE = "<value>"
# The module-level allowance by which a module states why a scope runs git status itself.
STATUS_ALLOWANCE = "STATUS_READ_ALLOWED"
READ_STATUS = (
    f"read a checkout's status through {STATUS_HELPER}'s changes, which passes --no-optional-locks so the read never "
    f"takes the checkout's index lock, or state why in {STATUS_ALLOWANCE}; see {POLICY}"
)


def _spelled(element: ast.expr, strings: Mapping[str, ast.Constant]) -> str | None:
    """The string an element spells, a literal or a name the module binds once to one, or None."""
    literal = strings.get(element.id) if isinstance(element, ast.Name) else element
    return None if literal is None else _string(literal)


def git_arguments(node: ast.expr, strings: Mapping[str, ast.Constant]) -> list[str] | None:
    """The arguments a list or a tuple display spells, up to its first starred element, each element that spells no
    string read as VALUE; None for anything else."""
    if not isinstance(node, ast.List | ast.Tuple):
        return None
    spelled: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Starred):
            break
        text = _spelled(element, strings)
        spelled.append(VALUE if text is None else text)
    return spelled


def separate_arguments(call: ast.Call, strings: Mapping[str, ast.Constant]) -> list[str]:
    """The run of a call's positional arguments that spell strings, from the first that does to the first after it
    that does not: git's arguments given one at a time, as `git(services, root, "status", "--porcelain")` gives them."""
    spelled: list[str] = []
    for argument in call.args:
        text = None if isinstance(argument, ast.Starred) else _spelled(argument, strings)
        if text is None:
            if spelled:
                break
            continue
        spelled.append(text)
    return spelled


def runs_status(call: ast.Call, strings: Mapping[str, ast.Constant]) -> ast.expr | None:
    """The argument at which a call gives git arguments whose subcommand is `status`, or None: a list or a tuple
    given by position, or a run of strings given one at a time with an option after the subcommand, since a word given
    alone, as `entry.get("status")`, is spelled alike."""
    for argument in call.args:
        listed = git_arguments(argument, strings)
        if listed is not None and git_client.subcommand(listed) == STATUS:
            return argument
    separate = separate_arguments(call, strings)
    index = git_client.subcommand_index(separate)
    if index is not None and separate[index] == STATUS and any(word.startswith("-") for word in separate[index + 1 :]):
        return call
    return None


def module_status_problems(name: str, tree: ast.Module) -> list[str]:
    """Each call in a module given git arguments that run `status`, as its line and its refusal, that the module's
    STATUS_READ_ALLOWED does not sanction, and each entry of it that sanctions nothing."""
    strings = _module_strings(tree)
    found: list[tuple[int, str, str]] = []
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.Call) and (at := runs_status(node, strings)) is not None:
            found.append((at.lineno, scope, f"{scope} runs git {STATUS} itself; {READ_STATUS}"))
    return sanctioned_problems(name, tree, STATUS_ALLOWANCE, found)


def status_modules(root: Path, files: Iterable[Path]) -> list[Path]:
    """The modules the status rule reads: every non-test module ruff formats but the helper."""
    return [root / name for name in formatted_modules(root, list(files)) if name != STATUS_HELPER]


def status_problems(root: Path, files: Iterable[Path] | None = None) -> list[str]:
    """Report each place a module other than the helper runs git status itself that it does not sanction, and each
    sanction that holds nothing."""
    listed = repository_files(root) if files is None else list(files)
    problems: list[str] = []
    for _, name, tree in python_trees(status_modules(root, listed), root, problems):
        problems += module_status_problems(name, tree)
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Pull request file listings

PULL_FILES_HELPER = "skills/ejryan89-code-review-core/scripts/review_github.py"
# The helper's reader of the listing, the class it answers with, and the checks a caller makes of that answer: each
# public method or property the class defines over its fields, read from the class's own definition.
PULL_FILES_READER = "list_pull_files"
PULL_FILES_CLASS = "PullFiles"
LISTING_CHECKS = public_members(REPOSITORY_ROOT / PULL_FILES_HELPER, PULL_FILES_CLASS)
# What a formatted value of an f-string reads as where the rule reads the text it spells.
FORMATTED = "\0"
# GitHub's files endpoint of a pull request, its number a literal or a formatted value, ending at a word end.
FILES_ENDPOINT = re.compile(rf"pulls/(?:{FORMATTED}|[0-9]+)/files(?![A-Za-z0-9_-])")
# The shepherd suite's test that holds its own reader of the endpoint to a whole listing at an unmoved head, which
# the fixture suite holds is there.
SHEPHERD_FILES_TEST = (
    ".claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::ReaderTest::"
    "test_files_that_cannot_be_read_whole_are_an_incomplete_answer"
)
# Each scope that reads a pull request's files itself, by its module's path and its scope, with the reason.
PULL_FILES_SANCTIONS: dict[str, dict[str, str]] = {
    ".claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py": {
        "every_file": "the shepherd reads pull requests through ejryan89-skill-core's gh client, not the review "
        "core's, and pages the endpoint only for a pull request gh's listing holds fewer files of than it changes; "
        "this policy holds only that the scope still reads the endpoint, and the shepherd's own suite, in "
        f"{SHEPHERD_FILES_TEST}, holds that it refuses a listing short of the count of changed files and one read "
        "while the head moved, as list_pull_files' callers do",
    },
}
READ_PULL_FILES = (
    f"read a pull request's files through {PULL_FILES_HELPER}'s {PULL_FILES_READER}, or name the scope with its "
    f"reason in {POLICY}'s PULL_FILES_SANCTIONS"
)


def spelled_text(node: ast.AST) -> str | None:
    """The text a string literal or an f-string spells, each formatted value read as FORMATTED; None for anything
    else."""
    if isinstance(node, ast.JoinedStr):
        return "".join(_string(part) or FORMATTED for part in node.values)
    return _string(node)


def _parents(tree: ast.Module) -> dict[int, ast.AST]:
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _bound_name(call: ast.Call, parents: Mapping[int, ast.AST]) -> str | None:
    """The one name a call's result is bound to, as `listing = github.list_pull_files(...)` binds it, or None."""
    parent = parents.get(id(call))
    targets = (
        parent.targets
        if isinstance(parent, ast.Assign)
        else [parent.target]
        if isinstance(parent, ast.AnnAssign | ast.NamedExpr)
        else []
    )
    return targets[0].id if len(targets) == 1 and isinstance(targets[0], ast.Name) else None


def _named(node: ast.expr) -> str | None:
    """The name a check is read of: a name, or the one name an assignment expression binds, as
    `(listing := github.list_pull_files(...)).cut` binds it."""
    if isinstance(node, ast.NamedExpr):
        node = node.target
    return node.id if isinstance(node, ast.Name) else None


def _reads_listing(call: ast.Call) -> bool:
    function = call.func
    name = (
        function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
    )
    return name == PULL_FILES_READER


def module_pull_files_problems(tree: ast.Module) -> list[tuple[int, str, str]]:
    """Each place a module spells GitHub's files endpoint of a pull request, and each call of the helper's reader whose
    listing its scope does not bind to a name and check with every one of LISTING_CHECKS, as its line, its scope, and
    its refusal."""
    parents = _parents(tree)
    checked: dict[str, dict[str, set[str]]] = {}
    calls: list[tuple[ast.Call, str]] = []
    found: list[tuple[int, str, str]] = []
    for node, scope in scoped_nodes(tree.body):
        if isinstance(parents.get(id(node)), ast.JoinedStr):
            continue
        text = spelled_text(node)
        if text is not None and FILES_ENDPOINT.search(text):
            refusal = f"{scope} spells GitHub's files endpoint of a pull request; {READ_PULL_FILES}"
            found.append((getattr(node, "lineno", 0), scope, refusal))
        if isinstance(node, ast.Call) and _reads_listing(node):
            calls.append((node, scope))
        if isinstance(node, ast.Attribute) and node.attr in LISTING_CHECKS and (named := _named(node.value)):
            checked.setdefault(scope, {}).setdefault(named, set()).add(node.attr)
    for call, scope in calls:
        bound = _bound_name(call, parents)
        if bound is None:
            refusal = f"{scope} reads {PULL_FILES_READER}'s listing without binding it to a name its checks read"
        else:
            missing = sorted(LISTING_CHECKS - checked.get(scope, {}).get(bound, set()))
            if not missing:
                continue
            refusal = (
                f"{scope} reads {PULL_FILES_READER}'s listing without checking {', '.join(missing)}, so a listing "
                f"cut short of the files GitHub counts, or one read at another head, is taken as the whole change"
            )
        found.append((call.lineno, scope, f"{refusal}; {READ_PULL_FILES}"))
    return sorted(set(found))


def pull_files_modules(root: Path, files: Iterable[Path]) -> list[Path]:
    """The modules the pull-files rule reads: every non-test module ruff formats but the helper."""
    return [root / name for name in formatted_modules(root, list(files)) if name != PULL_FILES_HELPER]


def pull_files_problems(
    root: Path, files: Iterable[Path] | None = None, sanctions: Mapping[str, Mapping[str, str]] = PULL_FILES_SANCTIONS
) -> list[str]:
    """Report each place a module other than the helper reads a pull request's files itself, or reads the helper's
    listing without checking it, that `sanctions` does not name, and each entry of it that sanctions nothing or gives
    no reason."""
    listed = repository_files(root) if files is None else list(files)
    problems: list[str] = []
    held: set[tuple[str, str]] = set()
    for _, name, tree in python_trees(pull_files_modules(root, listed), root, problems):
        for line, scope, refusal in module_pull_files_problems(tree):
            if scope in sanctions.get(name, {}):
                held.add((name, scope))
            else:
                problems.append(f"{name}:{line} {refusal}")
    for name, scopes in sanctions.items():
        for scope, reason in scopes.items():
            if not reason.strip():
                problems.append(
                    f"{POLICY}: PULL_FILES_SANCTIONS gives {name} {scope} no reason; state why it reads "
                    "a pull request's files itself"
                )
            elif (name, scope) not in held:
                problems.append(
                    f"{POLICY}: PULL_FILES_SANCTIONS names {name} {scope}, which reads no pull request's "
                    "files itself; remove the entry"
                )
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Snapshot paths

SNAPSHOT_HELPER = "skills/ejryan89-skill-core/scripts/diff_paths.py"
# The helper's judge of a name, which only its snapshot_path joins onto a snapshot's root.
SNAPSHOT_JUDGE = f"{diff_paths.__name__}.{diff_paths.unsafe_snapshot_name.__name__}"
SNAPSHOT_READER = f"{diff_paths.__name__}.{diff_paths.snapshot_path.__name__}"
# The joins of a path the rule reads besides `/`: a method of a path, and the functions of os.path's modules.
JOIN_METHOD = "joinpath"
JOIN_FUNCTIONS = frozenset({"os.path.join", "posixpath.join", "ntpath.join"})
SNAPSHOT_PATH_ALLOWANCE = "SNAPSHOT_PATH_READ_ALLOWED"
READ_SNAPSHOT_PATH = (
    f"judge a repository path read from a snapshot through {SNAPSHOT_READER}, which also holds it inside the "
    f"resolved root, or state why in {SNAPSHOT_PATH_ALLOWANCE}; see {POLICY}"
)


def _joined_operands(node: ast.AST, resolver: CallableResolver) -> list[ast.AST]:
    """The operands a node joins into a path: the two sides of a `/`, or the arguments of a joinpath or an os.path join;
    none for anything else."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [node.left, node.right]
    if isinstance(node, ast.Call):
        function = node.func
        if (isinstance(function, ast.Attribute) and function.attr == JOIN_METHOD) or resolver.name(
            function
        ) in JOIN_FUNCTIONS:
            return list(node.args)
    return []


def module_snapshot_path_problems(name: str, tree: ast.Module) -> list[str]:
    """Each place a scope joins a name it judged with the helper's unsafe_snapshot_name itself, as its line and its
    refusal, that the module's SNAPSHOT_PATH_READ_ALLOWED does not sanction, and each entry of it that sanctions
    nothing."""
    resolver = CallableResolver(tree)
    judged: dict[str, set[str]] = {}
    joins: list[tuple[ast.AST, str, list[ast.AST]]] = []
    for node, scope in scoped_nodes(tree.body):
        if (
            isinstance(node, ast.Call)
            and resolver.name(node.func) == SNAPSHOT_JUDGE
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            judged.setdefault(scope, set()).add(node.args[0].id)
        if operands := _joined_operands(node, resolver):
            joins.append((node, scope, operands))
    found: list[tuple[int, str, str]] = []
    for node, scope, operands in joins:
        names = {inner.id for operand in operands for inner in ast.walk(operand) if isinstance(inner, ast.Name)}
        if joined := sorted(names & judged.get(scope, set())):
            refusal = f"{scope} joins {', '.join(joined)}, a name it judges itself, onto a path; {READ_SNAPSHOT_PATH}"
            found.append((getattr(node, "lineno", 0), scope, refusal))
    return sanctioned_problems(name, tree, SNAPSHOT_PATH_ALLOWANCE, found)


def snapshot_path_modules(root: Path, files: Iterable[Path]) -> list[Path]:
    """The modules the snapshot-path rule reads: every non-test module ruff formats but the helper."""
    return [root / name for name in formatted_modules(root, list(files)) if name != SNAPSHOT_HELPER]


def snapshot_path_problems(root: Path, files: Iterable[Path] | None = None) -> list[str]:
    """Report each place a module other than the helper joins a snapshot path it judged itself that it does not
    sanction, and each sanction that holds nothing."""
    listed = repository_files(root) if files is None else list(files)
    problems: list[str] = []
    for _, name, tree in python_trees(snapshot_path_modules(root, listed), root, problems):
        problems += module_snapshot_path_problems(name, tree)
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Repository names

REPOSITORY_HELPER = "skills/ejryan89-skill-core/scripts/github_remote.py"
# The helper's class of the characters an owner and a name hold, read from the helper.
REPOSITORY_CLASS = f"[{github_remote.REPOSITORY_CHARACTERS}]"
# The characters a class is measured over: every ASCII character, control characters among them, and a letter
# outside it, so `[\w.-]`, which also takes that letter, and a class that adds a tab or a space are other classes.
CLASS_SAMPLE = "".join(map(chr, range(0x80))) + "\u00e9"
# A character class as a pattern writes one, its body escapes included, and what may stand between an owner's class
# and a name's: a quantifier, lazy or not, then a `/`, escaped or not.
CHARACTER_CLASS = re.compile(r"\[(?:\\.|[^\]\\])+\]")
OWNER_NAME_JOIN = re.compile(
    # A quantifier, lazy or not, a group's close, a `/`, escaped or not, then a group's open: plain, non-capturing, or
    # named.
    r"(?:[+*?]|\{[0-9]*,?[0-9]*\})\??\)?\\?/(?:\((?:\?:|\?P<[A-Za-z_][A-Za-z0-9_]*>)?)?"
)
REPOSITORY_ALLOWANCE = "REPOSITORY_PATTERN_ALLOWED"
READ_REPOSITORY = (
    f"build an owner/name pattern on {REPOSITORY_HELPER}'s REPOSITORY_CHARACTERS or OWNER_AND_NAME, or state why in "
    f"{REPOSITORY_ALLOWANCE}; see {POLICY}"
)


# What a class body may hold that `re` warns a later Python will read as a nested set or a set operation: no class
# that holds one is measured, so the rule never compiles a pattern a warning filter would turn into an error.
SET_OPERATIONS = ("[", "--", "&&", "~~", "||")


def class_members(spelled: str) -> frozenset[str] | None:
    """The characters of CLASS_SAMPLE a character class takes, or None for a class `re` cannot compile or that holds
    one of SET_OPERATIONS in its body."""
    if any(operation in spelled[1:-1] for operation in SET_OPERATIONS):
        return None
    try:
        compiled = re.compile(spelled)
    except re.error:
        return None
    return frozenset(character for character in CLASS_SAMPLE if compiled.fullmatch(character))


REPOSITORY_MEMBERS = class_members(REPOSITORY_CLASS)


def spells_owner_and_name(text: str) -> bool:
    """Whether a pattern's text spells an owner and a name with the helper's class: a class taking exactly the
    characters it takes, a quantifier, a `/`, and the same class again."""
    classes = [(found.start(), found.end()) for found in CHARACTER_CLASS.finditer(text)]
    repository = [class_members(text[start:end]) == REPOSITORY_MEMBERS for start, end in classes]
    return any(
        repository[index]
        and repository[index + 1]
        and OWNER_NAME_JOIN.fullmatch(text, classes[index][1], classes[index + 1][0]) is not None
        for index in range(len(classes) - 1)
    )


def module_repository_problems(name: str, tree: ast.Module) -> list[str]:
    """Each string literal in a module that spells an owner and a name with the helper's class itself, as its line and
    its refusal, that the module's REPOSITORY_PATTERN_ALLOWED does not sanction, and each entry of it that sanctions
    nothing."""
    found = [
        (node.lineno, scope, f"{scope} spells an owner/name pattern with its own character class; {READ_REPOSITORY}")
        for node, scope in scoped_nodes(tree.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and spells_owner_and_name(node.value)
    ]
    return sanctioned_problems(name, tree, REPOSITORY_ALLOWANCE, found)


def repository_modules(root: Path, files: Iterable[Path]) -> list[Path]:
    """The modules the repository-name rule reads: every non-test module ruff formats but the helper."""
    return [root / name for name in formatted_modules(root, list(files)) if name != REPOSITORY_HELPER]


def repository_problems(root: Path, files: Iterable[Path] | None = None) -> list[str]:
    """Report each owner/name pattern a module other than the helper spells with its own character class that it does
    not sanction, and each sanction that holds nothing."""
    listed = repository_files(root) if files is None else list(files)
    problems: list[str] = []
    for _, name, tree in python_trees(repository_modules(root, listed), root, problems):
        problems += module_repository_problems(name, tree)
    return problems


class OneHelperPolicies(unittest.TestCase):
    def test_every_path_to_a_place_lookup_treats_any_git_error_as_an_unknown_place(self) -> None:
        self.assertEqual([], place_lookup_problems(REPOSITORY_ROOT))

    def test_closing_references_are_read_only_through_their_one_helper(self) -> None:
        self.assertEqual([], closing_reference_problems(REPOSITORY_ROOT))

    def test_a_checkout_s_status_is_read_only_through_its_one_helper(self) -> None:
        self.assertEqual([], status_problems(REPOSITORY_ROOT))

    def test_the_status_rule_reads_every_module_its_rule_speaks_of(self) -> None:
        listed = repository_files(REPOSITORY_ROOT)
        modules = formatted_modules(REPOSITORY_ROOT, listed)
        read = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in status_modules(REPOSITORY_ROOT, listed)]
        self.assertEqual([], scope_problems("the status rule", modules, read, {STATUS_HELPER: "the one helper itself"}))

    def test_a_pull_request_s_files_are_read_only_through_their_one_helper(self) -> None:
        self.assertEqual([], pull_files_problems(REPOSITORY_ROOT))

    def test_a_snapshot_path_is_read_only_through_its_one_helper(self) -> None:
        self.assertEqual([], snapshot_path_problems(REPOSITORY_ROOT))

    def test_an_owner_and_name_pattern_is_built_only_on_its_one_helper(self) -> None:
        self.assertEqual([], repository_problems(REPOSITORY_ROOT))

    def test_the_pull_files_snapshot_path_and_repository_rules_read_every_module_their_rules_speak_of(self) -> None:
        listed = repository_files(REPOSITORY_ROOT)
        modules = formatted_modules(REPOSITORY_ROOT, listed)
        for policy, read_modules, helper in (
            ("the pull-files rule", pull_files_modules, PULL_FILES_HELPER),
            ("the snapshot-path rule", snapshot_path_modules, SNAPSHOT_HELPER),
            ("the repository-name rule", repository_modules, REPOSITORY_HELPER),
        ):
            with self.subTest(policy):
                read = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in read_modules(REPOSITORY_ROOT, listed)]
                self.assertEqual([], scope_problems(policy, modules, read, {helper: "the one helper itself"}))

    def test_the_closing_reference_rule_reads_every_module_its_rule_speaks_of(self) -> None:
        listed = repository_files(REPOSITORY_ROOT)
        modules = formatted_modules(REPOSITORY_ROOT, listed)
        read = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in closing_modules(REPOSITORY_ROOT, listed)]
        self.assertEqual(
            [], scope_problems("the closing-reference rule", modules, read, {HELPER: "the one helper itself"})
        )

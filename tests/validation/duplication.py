"""No definition is copied between files, and shared code lives in ejryan89-skill-core and is imported from there:
among it the one containment test of a path in a tree.
"""

from __future__ import annotations

import ast
import functools
import re
import string
import sys
import unittest
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

import file_presence
import whole_number
from path_containment import posix_within
from python_checks import PATH_CONTAINMENT, claim_files, formatted_modules, sanctioned_problems
from skill_roots import SKILL_CORE
from validation_support import (
    COMMON_PATH_FUNCTIONS,
    DEFINITIONS,
    MODULE_LOADERS,
    PATH_MODULES,
    REPOSITORY_ROOT,
    VALIDATION_DOC,
    CallableResolver,
    call_argument,
    import_aliases,
    is_test_script,
    listed,
    module_allowance,
    module_imports,
    module_statements,
    python_source,
    python_trees,
    qualified_name,
    repository_files,
    scope_problems,
    scoped_nodes,
    scripts_put_on_path,
    skill_dependency_closure,
)

INDIRECTION_ALLOWED = {
    "_flags": "reads the value of the re flag a pattern's call names, a member of re.RegexFlag, and runs no code by "
    "its name",
}
DUPLICATION_ALLOWED = {
    "name_key": "the `.git` matcher compares parsed literals with `.git`, the literal it looks for, and names no "
    "folder on any file system, so skills/ejryan89-skill-core/scripts/path_keys.py has no key to give it",
}
# A module sanctions a copy beside the code it excuses, as a module-level DUPLICATION_ALLOWED = {name: reason}, and
# every module holding the copy states it under its own name for the copy. Nothing sanctions a whole module: shared
# code lives in ejryan89-skill-core. An allowance the module no longer needs is reported.
DUPLICATION_ALLOWANCE = "DUPLICATION_ALLOWED"
# Every tree of the repository's own Python but its suites and fixtures: the shipped skills, the deployer, the tools,
# the repository skills, which meet the shipped-skill standard, and the policies, which share what they repeat through
# validation_support.
DUPLICATION_ROOTS = ("skills", "deployer", "tools", ".claude/skills", "tests/validation")
MINIMUM_DUPLICATED_LINES = 4
# A string literal shorter than this is a common word or file name, such as "unknown" or "run.json", that two modules
# may hold for separate decisions; a collection of literals is held to it by the characters its items hold together.
# A compiled pattern has no floor, because any pattern states a format.
MINIMUM_CONSTANT_LENGTH = 12
# __all__ lists a module's own exports, and another module may export the same names.
UNCOMPARED_CONSTANTS = frozenset({"__all__"})
# A sanction dictionary, such as PLATFORM_ALLOWED or DUPLICATION_ALLOWED itself, names what its module excuses, and
# another module may excuse the same thing. Only a dictionary under such a name is one; any other value is compared.
SANCTION_SUFFIX = "_ALLOWED"
# An integer is compared only when written as a bit pattern, in hexadecimal, octal, or binary, such as a Windows
# process-creation flag or a file mode: a value an operating system or a format defines, which two modules holding it
# under any names most likely both copied. A decimal integer is a count, a limit, an exit code, or a version, which
# modules choose for separate decisions and share by chance, as several record schemas each start at version 1 and
# several tools wait 120 seconds, so no decimal integer is compared; nor is a bit pattern written in decimal, or built
# from others, as `A | B` is. A bit pattern is keyed by its value alone, so two unrelated ones that share a value in
# different modules, such as two interfaces' 0x1 flags, are refused as one copy and sanctioned with
# DUPLICATION_ALLOWED in each module; within one module, as job_tree.py's two 0x4 flags, nothing is compared.
BIT_PATTERN_PREFIXES = ("0x", "0o", "0b")
# A constant's group key starts with this, so a constant and a definition never share a group.
CONSTANT_KEY = "constant:"
# The calls that build a collection from a literal. A collection compares by its members in any order, so a tuple and a
# set of one vocabulary, such as the severities a record allows, are one decision however each module holds it.
COLLECTION_CALLS = frozenset({"tuple", "list", "set", "frozenset"})

# The names that tell a name grammar from another format: lowercase runs joined by hyphens, a digit, one character,
# the length limit, and the forms every such grammar refuses, a leading or trailing hyphen, a capital, an underscore,
# a space, and the empty name. No probe holds consecutive hyphens, deliberately: a grammar that allows them is the
# same grammar drifted, as the deployer's once was, and is a copy all the same.
NAME_GRAMMAR_PROBES = ("pdf-processing", "a", "data-2", "a" * 64, "-pdf", "pdf-", "PDF", "pdf_processing", "pdf x", "")

# A restatement of a shared fact's vocabulary holds at least this many of its members: fewer is a command that passes
# one of them, such as a git invocation with -C.
MINIMUM_SHARED_MEMBERS = 3

Copy = tuple[str, int, str]
# A shared fact's restatement test: the node, the fact's members as its owner holds them, whether the node's module
# imports the owner, the fact's name, which a module that imports it reads the fact through, and the module's
# resolver, which names what a call calls through the module's imports.
Restates = Callable[[ast.AST, frozenset[str], bool, str, CallableResolver], bool]


def duplication_omissions() -> dict[str, str]:
    """The parts of the formatted Python the duplication policy leaves out, each with its reason."""
    reason = (
        "outside the trees the rule names: the suites and their support state what they test as literals of their "
        "own, and the root modules share nothing a tree imports"
    )
    return {"tests": reason, "deploy.py": reason, "conftest.py": reason}


def _definition_key(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """The definition's syntax tree without docstrings or line numbers, equal for two copies of one body."""
    for child in ast.walk(node):
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and isinstance(child.body[0], ast.Expr)
            and isinstance(child.body[0].value, ast.Constant)
            and isinstance(child.body[0].value.value, str)
        ):
            child.body = child.body[1:] or [ast.Pass()]
    return ast.dump(node)


def _flags(node: ast.expr | None, resolver: CallableResolver) -> int | str:
    """A pattern's flags by value, so re.I and re.IGNORECASE, in either order, compare equal; a flag the policy
    cannot read compares by its syntax."""
    if node is None:
        return 0
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _flags(node.left, resolver), _flags(node.right, resolver)
        if isinstance(left, int) and isinstance(right, int):
            return left | right
    name = resolver.name(node)
    flag = getattr(re, name.removeprefix("re."), None) if name.startswith("re.") else None
    return int(flag) if isinstance(flag, re.RegexFlag) else ast.dump(node)


def _literals(node: ast.expr) -> list[str | bytes] | None:
    """The items of a tuple, list, or set literal that holds only string or bytes literals, or None."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    items: list[str | bytes] = [
        item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, (str, bytes))
    ]
    return items if len(items) == len(node.elts) else None


def _is_bit_pattern(value: ast.Constant, source: Callable[[], str]) -> bool:
    """Whether an integer literal is written in hexadecimal, octal, or binary, read from its source text, since the
    syntax tree keeps only the value."""
    literal = ast.get_source_segment(source(), value) or ""
    return literal[:2].lower() in BIT_PATTERN_PREFIXES


def _constant_key(value: ast.expr, resolver: CallableResolver, source: Callable[[], str]) -> str | None:
    """The group key of a module-level value the policy compares, or None for any other value: a long string or bytes
    literal, an integer written as a bit pattern, a pattern compiled from a literal, or a collection of literals that
    together are long. An integer's key is its value, so 0x1A4 and 0o644 are one copy."""
    if isinstance(value, ast.Constant) and type(value.value) is int:
        return f"{CONSTANT_KEY}{value.value!r}" if _is_bit_pattern(value, source) else None
    if isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)):
        return f"{CONSTANT_KEY}{value.value!r}" if len(value.value) >= MINIMUM_CONSTANT_LENGTH else None
    called = resolver.name(value.func) if isinstance(value, ast.Call) else ""
    if isinstance(value, ast.Call) and called == "re.compile":
        pattern = call_argument(value, 0, "pattern")
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, (str, bytes)):
            return None
        return f"{CONSTANT_KEY}re.compile{(pattern.value, _flags(call_argument(value, 1, 'flags'), resolver))!r}"
    if isinstance(value, ast.Call) and called in COLLECTION_CALLS and len(value.args) == 1 and not value.keywords:
        value = value.args[0]
    items = _literals(value)
    if not items or sum(len(item) for item in items) < MINIMUM_CONSTANT_LENGTH:
        return None
    return f"{CONSTANT_KEY}{tuple(sorted(set(items), key=repr))!r}"


def _module_copies(name: str, tree: ast.Module, path: Path) -> Iterator[tuple[str, Copy]]:
    """Each definition and constant the module may share with another, as its group key and where it is."""
    resolver = CallableResolver(tree)
    # The source is read again only for a module that binds an integer, whose literal's form the tree does not keep.
    source = functools.cache(lambda: python_source(path))
    for node in module_statements(tree.body):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and (node.end_lineno or node.lineno) - node.lineno + 1 >= MINIMUM_DUPLICATED_LINES
        ):
            yield _definition_key(node), (name, node.lineno, node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = [
                target.id
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name)
                and target.id not in UNCOMPARED_CONSTANTS
                and not (target.id.endswith(SANCTION_SUFFIX) and isinstance(node.value, ast.Dict))
            ]
            key = _constant_key(node.value, resolver, source) if targets else None
            if key is not None:
                yield from ((key, (name, node.lineno, target)) for target in targets)


def _copy_report(key: str, group: list[Copy]) -> str:
    fix = f"import one copy, or sanction it in each copy's module with {DUPLICATION_ALLOWANCE}"
    if key.startswith(CONSTANT_KEY):
        held = listed([f"{copy} at {module}:{line}" for module, line, copy in group], "and")
        return f"{held} hold the same value; {fix}"
    places = listed([f"{module}:{line}" for module, line, _ in group], "and")
    return f"{group[0][2]} is defined identically in {places}; {fix}"


def _shares_members(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A tuple, list, or set literal holding several of the fact's members: a copy of its vocabulary, however far it
    has drifted from the owner's."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    held = {item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return len(held & members) >= MINIMUM_SHARED_MEMBERS


def _shares_call_names(
    node: ast.AST, members: frozenset[str], imports_owner: bool, name: str, resolver: CallableResolver
) -> bool:
    """A tuple, list, or set literal holding several of the fact's calls, each by its dotted name or by the last part
    of it, as a reader that resolves no imports names them: `{"exit", "_exit", "SystemExit"}` is a copy of the calls
    that exit as much as the dotted names are."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    last_parts = frozenset(member.rpartition(".")[2] for member in members)
    held = ast.Set(
        elts=[
            ast.Constant(item.value.rpartition(".")[2])
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    )
    return _shares_members(held, last_parts, imports_owner, name, resolver)


def _holds_member(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A string literal holding one of the fact's members, as a glob or a message re-derives a rule from it."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(member in node.value for member in members)
    )


def _equals_in_importer(
    node: ast.AST, members: frozenset[str], imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """The fact's own value as a literal in a module that imports its owner, which could import the value too. A
    short value such as a file name recurs for unrelated reasons elsewhere, so only an importer is read."""
    return imports_owner and isinstance(node, ast.Constant) and node.value in members


def _states_number(
    node: ast.AST, members: frozenset[str], imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A limit's own number as a literal in a module that imports its owner, or stated as "at most" the number in any
    text, as a refusal's message re-derives it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return imports_owner and str(node.value) in members
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(f"at most {member}" in node.value for member in members)
    )


def _names_key(node: ast.AST, members: frozenset[str], name: str) -> bool:
    """The key as a literal, or the owner's name for it, as a module reads it."""
    if isinstance(node, ast.Constant):
        return node.value in members
    return (isinstance(node, ast.Name) and node.id == name) or (isinstance(node, ast.Attribute) and node.attr == name)


def _reads_key(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, name: str, resolver: CallableResolver
) -> bool:
    """A reading of a key outside its owner's: the key's literal passed to a call, as a reader looks it up, or the key
    compared, after any call, with true or false, as a reader judges its value, by an operator or by a match statement
    on it whose case holds true or false as a value."""
    if isinstance(node, ast.Call):
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        return any(isinstance(argument, ast.Constant) and argument.value in members for argument in arguments)
    if isinstance(_module_parents(resolver).get(id(node)), ast.MatchValue):
        subject = _match_subject(node, resolver)
        return _is_truth_word(node) and any(_names_key(inner, members, name) for inner in ast.walk(subject))
    if not isinstance(node, ast.Compare):
        return False
    operands = [node.left, *node.comparators]
    judged = any(_is_truth_word(operand) for operand in operands)
    return judged and any(_names_key(inner, members, name) for operand in operands for inner in ast.walk(operand))


def _is_truth_word(node: ast.AST) -> bool:
    """Whether the node is a string literal reading true or false, in any case and with any surrounding space."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().casefold() in {"true", "false"}
    )


def _patterns_member(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A string literal holding a member as a regular expression escapes it, as a pattern re-derives a spelling; a
    member that needs no escape reads alike in prose and is left to the prose."""
    escaped = {re.escape(member) for member in members} - members
    return (
        isinstance(node, ast.Constant) and isinstance(node.value, str) and any(form in node.value for form in escaped)
    )


def _bound_text(node: ast.Name, resolver: CallableResolver, seen: frozenset[int]) -> str | None:
    """The text a name gives when its scope binds it exactly once, to an expression whose text is static."""
    owner = resolver.scopes[id(node)].owner(node.id)
    values = owner.bindings[node.id] if owner is not None else []
    if len(values) != 1 or not isinstance(values[0], ast.expr) or id(values[0]) in seen:
        return None
    return _static_text(values[0], resolver, seen | {id(values[0])})


def _formatted_text(part: ast.FormattedValue, resolver: CallableResolver, seen: frozenset[int]) -> str | None:
    """The text an f-string's placeholder gives: its static value after its conversion and its format specification,
    which may hold placeholders of its own."""
    value = _static_text(part.value, resolver, seen)
    spec = _static_text(part.format_spec, resolver, seen) if part.format_spec is not None else ""
    if value is None or spec is None:
        return None
    converted = {ord("r"): repr, ord("a"): ascii}.get(part.conversion, str)(value)
    try:
        return format(converted, spec)
    except ValueError:
        return None


def _static_items(nodes: list[ast.expr], resolver: CallableResolver, seen: frozenset[int]) -> list[str] | None:
    """The texts of expressions whose every text is static, or None."""
    items: list[str] = []
    for node in nodes:
        text = _static_text(node, resolver, seen)
        if text is None:
            return None
        items.append(text)
    return items


def _static_mapping(
    keys: list[ast.expr | None], values: list[ast.expr], resolver: CallableResolver, seen: frozenset[int]
) -> dict[str, str] | None:
    """The texts of keys and their values whose every text is static, or None; a key of None is an unpacking."""
    mapping: dict[str, str] = {}
    for key, value in zip(keys, values, strict=True):
        name = _static_text(key, resolver, seen) if key is not None else None
        text = _static_text(value, resolver, seen)
        if name is None or text is None:
            return None
        mapping[name] = text
    return mapping


def _interpolated_text(node: ast.BinOp, resolver: CallableResolver, seen: frozenset[int]) -> str | None:
    """The text a `%` gives when its template and every value it fills in are static: one value, a literal tuple of
    them, or a literal dictionary of them under literal string keys."""
    template = _static_text(node.left, resolver, seen)
    right = node.right
    values: str | tuple[str, ...] | dict[str, str] | None
    if isinstance(right, ast.Dict):
        values = _static_mapping(right.keys, right.values, resolver, seen)
    elif isinstance(right, ast.Tuple):
        items = _static_items(right.elts, resolver, seen)
        values = tuple(items) if items is not None else None
    else:
        values = _static_text(right, resolver, seen)
    if template is None or values is None:
        return None
    try:
        return template % values
    except (TypeError, ValueError, KeyError):
        return None


def _called_text(node: ast.Call, resolver: CallableResolver, seen: frozenset[int]) -> str | None:
    """The text a string method call gives when its receiver and arguments are static: `.format` of static values,
    by position or keyword, and `.join` of a literal tuple or list."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in {"format", "join"}:
        return None
    receiver = _static_text(node.func.value, resolver, seen)
    if receiver is None:
        return None
    if node.func.attr == "join":
        joined = node.args[0] if len(node.args) == 1 else None
        items = _static_items(joined.elts, resolver, seen) if isinstance(joined, (ast.Tuple, ast.List)) else None
        return receiver.join(items) if items is not None else None
    arguments = _static_items(node.args, resolver, seen)
    # An unpacking's keyword is None, a constant with no text.
    names: list[ast.expr | None] = [ast.Constant(keyword.arg) for keyword in node.keywords]
    keywords = _static_mapping(names, [keyword.value for keyword in node.keywords], resolver, seen)
    if arguments is None or keywords is None:
        return None
    try:
        return receiver.format(*arguments, **keywords)
    except (IndexError, KeyError, ValueError, AttributeError, TypeError):
        return None


def _static_text(node: ast.expr, resolver: CallableResolver, seen: frozenset[int] = frozenset()) -> str | None:
    """The string an expression gives when it is static, or None: a string literal; a name its scope binds exactly
    once, to an expression whose text is static, through the module's own bindings only; and the `+`, `%`, f-string,
    `.format`, or `.join` of static texts. Seen holds the bindings already followed, so a name bound to itself ends.

    It does not follow a name its scope binds twice, in branches of an if or a try or by an augmented assignment, or
    binds by a loop, a with, or a parameter with no default; a name imported from another module; an attribute, a
    subscript, or a function's result; `*` repetition, a comprehension joined, or any other string method; a `%` or a
    `.format` given an unpacking; or a bytes pattern. Each such expression has no static text here."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return _bound_text(node, resolver, seen)
    if isinstance(node, ast.JoinedStr):
        parts = _static_items(node.values, resolver, seen)
        return "".join(parts) if parts is not None else None
    if isinstance(node, ast.FormattedValue):
        return _formatted_text(node, resolver, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left, resolver, seen), _static_text(node.right, resolver, seen)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _interpolated_text(node, resolver, seen)
    if isinstance(node, ast.Call):
        return _called_text(node, resolver, seen)
    return None


def _grammar_text(node: ast.AST, resolver: CallableResolver) -> str | None:
    """The text the name grammar's guard compiles at a node: the static pattern of a call PATTERN_CALLS names, or the
    static text of any other expression when it names the range a-z. A name's read and an f-string's placeholder are
    left out: what they give is judged where it is bound or in the string around it, and a message that quotes a
    grammar already judged restates nothing."""
    if isinstance(node, ast.Call) and resolver.name(node.func) in PATTERN_CALLS:
        pattern = call_argument(node, 0, "pattern")
        return _static_text(pattern, resolver) if pattern is not None else None
    if not isinstance(node, ast.expr) or isinstance(node, (ast.Name, ast.FormattedValue)):
        return None
    text = _static_text(node, resolver)
    return text if text is not None and "a-z" in text else None


def _derives_name_grammar(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, resolver: CallableResolver
) -> bool:
    """A pattern that, compiled, judges every name in NAME_GRAMMAR_PROBES as the fact's pattern does: the name grammar
    re-derived, however it treats `--`, which no probe holds, as the deployer's copy once drifted from
    audit-ai-config's.

    The guard compiles the static text of each regular-expression call's pattern, whatever range it spells, and of
    each string literal and each `+`, `%`, f-string, `.format`, or `.join` that names the range a-z, so a grammar built
    from pieces, or from a piece bound to a name and joined later, is compiled whole; _static_text names what it
    follows. Stated limits: a pattern _static_text cannot read, such as a piece imported from another module, a name
    bound twice, or a piece a function returns, is not compiled; nor is a static text that spells the range another
    way, as a class listing every letter does, unless a regular-expression call in its own module is given it, by a
    name or whole. A call's flags are not applied: the text restates the grammar whatever flags compile it."""
    text = _grammar_text(node, resolver)
    if text is None:
        return False
    try:
        candidate = re.compile(text)
    except re.error:
        return False
    owners = [re.compile(member) for member in members]
    return any(
        all((candidate.fullmatch(probe) is None) == (owner.fullmatch(probe) is None) for probe in NAME_GRAMMAR_PROBES)
        for owner in owners
    )


def _reads_title_or_body(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A pull request title or body file read with a codec of its own: read_text or open given an encoding other than
    the owner's, on a path whose expression names a title or a body."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
        path, encoding = node.func.value, call_argument(node, 0, "encoding")
    elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
        path, encoding = node.args[0], call_argument(node, 2, "encoding")
    else:
        return False
    named = any(word in ast.unparse(path).casefold() for word in ("title", "body"))
    return named and isinstance(encoding, ast.Constant) and encoding.value not in members


def _reads_version_numbers(
    node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str, _resolver: CallableResolver
) -> bool:
    """A version's numbers read by hand: int applied to each part of a text split on the owner's separator, in a
    comprehension or through map, as a version parser re-derives the shared reader."""
    if isinstance(node, (ast.GeneratorExp, ast.ListComp)):
        converted = isinstance(node.elt, ast.Call) and isinstance(node.elt.func, ast.Name) and node.elt.func.id == "int"
        parts = [generator.iter for generator in node.generators]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "map" and node.args:
        first = node.args[0]
        converted = isinstance(first, ast.Name) and first.id == "int"
        parts = node.args[1:2]
    else:
        return False
    return converted and any(
        isinstance(part, ast.Call)
        and isinstance(part.func, ast.Attribute)
        and part.func.attr == "split"
        and isinstance(separator := call_argument(part, 0, "sep"), ast.Constant)
        and separator.value in members
        for part in parts
    )


def _called_name(node: ast.AST) -> str:
    """The method a call calls, such as `resolve` for `path.resolve()`, or "" for any other call or node."""
    return node.func.attr if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else ""


def _folds_path_case(
    node: ast.AST, _members: frozenset[str], _imports_owner: bool, _name: str, resolver: CallableResolver
) -> bool:
    """A path's or a name's comparison key built by hand: anything case-folded through os.path.normcase, which folds
    by the platform's name, never the file system's, so a name such as a folder's against `.git` is refused too and
    compared through path_keys' name_key, which asks the folder's file system; or a path made absolute, normalized, or
    resolved, then case-folded through str.casefold or str.lower, which fold case even where the file system keeps
    it, path_containment's `resolved_path` and `strictly_resolved_path` among the resolutions. A function is named
    through the module's imports, so an alias of normcase is normcase and a local function
    that shares its name is not; a method is named by its name, since its receiver's type is unknown. str.casefold and
    str.lower of other text are left alone, since most such text is no path, and a name folded that way folds on every
    file system, which merges names rather than telling one file system from another."""
    folds = {"os.path.normcase", "posixpath.normcase", "ntpath.normcase"}
    wrappers = {"str", "pathlib.Path", "pathlib.PurePath", "os.fspath"}
    normalizers = {
        f"{module}.{name}"
        for module in ("os.path", "posixpath", "ntpath")
        for name in ("normpath", "abspath", "realpath")
    } | {f"path_containment.{name}" for name in ("resolved_path", "strictly_resolved_path")}
    if not isinstance(node, ast.Call):
        return False
    called = resolver.name(node.func)
    if called in folds and node.args:
        folded = node.args[0]
    elif _called_name(node) in {"casefold", "lower"} and not node.args:
        folded = node.func.value if isinstance(node.func, ast.Attribute) else node
    else:
        return False
    while isinstance(folded, ast.Call) and resolver.name(folded.func) in wrappers and folded.args:
        folded = folded.args[0]
    if called in folds:
        return True
    return isinstance(folded, ast.Call) and (
        resolver.name(folded.func) in normalizers or _called_name(folded) in {"resolve", "absolute"}
    )


def _compares_git_name(
    node: ast.AST, _members: frozenset[str], _imports_owner: bool, _name: str, resolver: CallableResolver
) -> bool:
    """A name compared with `.git` by hand, which misses a `.GIT` a folding file system takes for `.git` when the test
    is exact, or takes it on a case-sensitive one too after a str.casefold. name_key asks the folder's file system, so
    `.GIT` is the Git directory only where the folder folds case, and a comparison of its result is never refused;
    path_keys' opens_as, which asks the folder for the entry it gives under `.git`, as folder_walk's
    `is_checkout_marker` does, compares nothing and is never refused either.

    `.git` is the literal, a module-level name the module binds once to it, such as `GIT = ".git"`, or an attribute
    a module-level class binds once to it in its body, such as `Names.GIT`. The forms refused: `==` or `!=` between
    `.git` and anything but name_key's result; a membership test of anything but that result in a list, tuple, or set
    display holding `.git`, or in frozenset, set, tuple, or list called on one, or of `.git` itself in anything, as
    `".git" in parts`; str.startswith or str.endswith given `.git`, alone or in such a collection; and a match
    statement on anything but name_key's result with a case whose pattern holds `.git` as a value, as `case ".git":`,
    `case ".git" | ".hg":`, `case [*_, ".git"]:`, or `case Names.GIT:` do. A bare name in a case captures the subject
    rather than comparing it, so a held name reaches a case only through a class's attribute. A comparison or a
    membership test joined by `and` to a name_key comparison with `.git` only narrows what the folder is asked about,
    and is not refused; a startswith or endswith call, and a case whose guard asks name_key, are refused even there.

    The limit: membership in a collection held under a module-level name, such as a walk's skip list
    `frozenset({".git", "node_modules"})`, is not read, since the collection's other names are matched exactly by
    design and the policy cannot tell a skip list from a test for the Git directory."""
    gits = _held_git_names(resolver)
    if isinstance(node, ast.Call):
        return _calls_git_affix(node, resolver, gits)
    if isinstance(_module_parents(resolver).get(id(node)), ast.MatchValue):
        return _is_git(node, gits) and not _keyed_by_folder(_match_subject(node, resolver), resolver)
    if not isinstance(node, ast.Compare) or _narrows_keyed_test(node, resolver, gits):
        return False
    return any(_exact_git_test(test, resolver, gits) for test in _comparisons(node))


# The Git directory's name, which _compares_git_name looks for as a literal.
GIT_DIRECTORY = ".git"
# The str methods that test a name's start or end, which a `.git` test may be written with.
AFFIX_TESTS = frozenset({"startswith", "endswith"})


def _comparisons(node: ast.Compare) -> Iterator[tuple[ast.cmpop, ast.expr, ast.expr]]:
    """Each operator of a comparison chain with its two operands."""
    operands = [node.left, *node.comparators]
    for index, operator in enumerate(node.ops):
        yield operator, operands[index], operands[index + 1]


def _is_git(node: ast.AST, gits: frozenset[str]) -> bool:
    """Whether the node is `.git`: the literal, a module-level name the module binds once to it, or a module-level
    class's attribute bound once to it."""
    if isinstance(node, ast.Constant):
        return node.value == GIT_DIRECTORY
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}" in gits
    return isinstance(node, ast.Name) and node.id in gits


def _match_subject(node: ast.AST, resolver: CallableResolver) -> ast.expr:
    """The subject of the match statement whose case pattern holds the node."""
    parents = _module_parents(resolver)
    holder = parents[id(node)]
    while not isinstance(holder, ast.Match):
        holder = parents[id(holder)]
    return holder.subject


def _exact_git_test(
    test: tuple[ast.cmpop, ast.expr, ast.expr], resolver: CallableResolver, gits: frozenset[str]
) -> bool:
    """Whether one comparison tests a name against `.git` by hand."""
    operator, first, second = test
    if isinstance(operator, (ast.Eq, ast.NotEq)):
        return any(
            _is_git(side, gits) and not _keyed_by_folder(other, resolver)
            for side, other in ((first, second), (second, first))
        )
    if isinstance(operator, (ast.In, ast.NotIn)):
        listed_git = any(_is_git(member, gits) for member in _collection_members(second, resolver) or [])
        return _is_git(first, gits) or (listed_git and not _keyed_by_folder(first, resolver))
    return False


def _calls_git_affix(node: ast.Call, resolver: CallableResolver, gits: frozenset[str]) -> bool:
    """Whether the call is str.startswith or str.endswith given `.git`, alone or in a collection."""
    if _called_name(node) not in AFFIX_TESTS or not node.args:
        return False
    affix = node.args[0]
    affixes = _collection_members(affix, resolver) or [affix]
    return any(_is_git(member, gits) for member in affixes)


# The builtins that build a collection from the display they are given, as `frozenset({".git"})` does.
COLLECTION_BUILDERS = frozenset({"frozenset", "set", "tuple", "list"})


def _collection_members(node: ast.expr, resolver: CallableResolver) -> list[ast.expr] | None:
    """The members of a list, tuple, or set display, or of one a collection builtin is called on, through any number
    of such calls; None for anything else."""
    while (
        isinstance(node, ast.Call)
        and resolver.name(node.func) in COLLECTION_BUILDERS
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return list(node.elts) if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else None


def _narrows_keyed_test(node: ast.Compare, resolver: CallableResolver, gits: frozenset[str]) -> bool:
    """Whether the comparison is an operand of an `and` that also compares name_key's result with `.git`, so the exact
    test only spares the folder a question and name_key decides."""
    parent = _module_parents(resolver).get(id(node))
    if not (isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And)):
        return False
    return any(
        isinstance(value, ast.Compare)
        and any(
            isinstance(operator, (ast.Eq, ast.NotEq))
            and any(_is_git(side, gits) and _keyed_by_folder(other, resolver) for side, other in ((a, b), (b, a)))
            for operator, a, b in _comparisons(value)
        )
        for value in parent.values
        if value is not node
    )


@functools.lru_cache(maxsize=1)
def _module_parents(resolver: CallableResolver) -> dict[int, ast.AST]:
    """Each node of the resolver's module by identity, with the node that holds it."""
    return {id(child): parent for parent in ast.walk(resolver.tree) for child in ast.iter_child_nodes(parent)}


@functools.lru_cache(maxsize=1)
def _held_git_names(resolver: CallableResolver) -> frozenset[str]:
    """The module-level names the resolver's module binds once, and to the `.git` literal, and each attribute a
    module-level class binds once to it in its body, by its dotted name, such as `Names.GIT`."""
    held = set(_bound_to_git(resolver.tree.body))
    for statement in module_statements(resolver.tree.body):
        if isinstance(statement, ast.ClassDef):
            held |= {f"{statement.name}.{name}" for name in _bound_to_git(statement.body)}
    return frozenset(held)


def _bound_to_git(body: list[ast.stmt]) -> Iterator[str]:
    """The names a block binds once, and to the `.git` literal, counting a binding in its if, try, or with blocks."""
    bound: dict[str, list[ast.expr | None]] = {}
    for statement in module_statements(body):
        pairs: list[tuple[ast.expr, ast.expr | None]]
        if isinstance(statement, ast.Assign):
            pairs = [(target, statement.value) for target in statement.targets]
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            pairs = [(statement.target, statement.value)]
        elif isinstance(statement, ast.AugAssign):
            # An augmented assignment binds the name again, to a value no literal shows.
            pairs = [(statement.target, None)]
        else:
            continue
        for target, value in pairs:
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).append(value)
    return (
        name
        for name, values in bound.items()
        if len(values) == 1 and isinstance(values[0], ast.Constant) and values[0].value == GIT_DIRECTORY
    )


def _keyed_by_folder(node: ast.AST, resolver: CallableResolver) -> bool:
    """Whether `node` calls name_key, from path_keys or through the deployer's platform_support, which re-exports it."""
    if not isinstance(node, ast.Call):
        return False
    module, _, function = resolver.name(node.func).rpartition(".")
    return function == "name_key" and module.rpartition(".")[2] in {"path_keys", "platform_support"}


# The characters a fence's run repeats three or more times, and a regular expression's class that repeats one of them
# three or more times, as in a class of both followed by `{3,}`, built from them so this policy holds no run itself.
FENCE_MARKERS = ("`", "~")
FENCE_CLASS_RUN = re.compile(r"\[[^\]]*[" + "".join(FENCE_MARKERS) + r"][^\]]*\]\{3")
# The re functions that take a pattern first, as `pattern`: a fence run in that pattern finds or tests a fence, and the
# name grammar's guard compiles that pattern's static text whatever range it spells.
PATTERN_CALLS = frozenset(
    f"re.{name}" for name in ("compile", "match", "fullmatch", "search", "findall", "finditer", "sub", "subn", "split")
)
# The comparisons that test a line against a run: whether it holds it, or is it.
RUN_TESTS = (ast.In, ast.NotIn, ast.Eq, ast.NotEq)
# The str methods that find, count, split at, or take off a run given first, as `text.split(run)` splits a document at
# its fences or `line.find(run) == 0` tests where a line's run starts.
RUN_METHODS = frozenset(
    {
        "split",
        "rsplit",
        "find",
        "rfind",
        "index",
        "rindex",
        "partition",
        "rpartition",
        "count",
        "removeprefix",
        "removesuffix",
    }
)
# The str methods that strip the characters given first, so a marker among them strips a run of any length, which
# reads a line's run where the result is compared with the line, as `line.lstrip("`") != line` tests whether a line
# starts with one, or measured against it, as `len(line) - len(line.lstrip("`"))` counts the run.
STRIP_METHODS = frozenset({"strip", "lstrip", "rstrip"})


def _reads_fence_run(
    node: ast.AST, _members: frozenset[str], _imports_owner: bool, _name: str, resolver: CallableResolver
) -> bool:
    """A fence read by hand beside markdown_sections.find_fences: a fence's run, as a string literal or a name the
    module binds to one, alone or in a tuple, given to str.startswith or str.endswith as a reader tests a line's start
    or end, given first to a str method that finds, counts, splits at, or takes off a run, as `text.split("```")` or
    `line.find("~~~")` does, given first to an re function as its pattern, however re or the function is imported, or
    compared with `in`, `not in`, `==`, or `!=`, as `"```" in line` or `line[:3] == "~~~"` test a line; or a marker
    given to str.strip, lstrip, or rstrip, which strips a run of any length, whose result is compared with what it
    stripped or its length taken from that one's, as `line.lstrip("`") != line` or
    `len(line) - len(line.lstrip("`"))` reads a line's run, where a strip of a cell or a name, as
    `cell.strip("`")`, reads none. Such a reader misses a fence a blockquote or a list item holds and takes any line
    that starts with a run, an info string's included, as the fence's close. Stated limits: a run built at runtime,
    such as from a marker by `*`, a run given by keyword, as `text.split(sep="```")`, or to a str method called
    unbound, as `str.find(line, "```")`, a run as a `case` pattern, as `case "```":` of `match line[:3]:`, and a strip
    bound to a name, by an assignment or an assignment expression, before it is measured or compared, as
    `stripped = line.lstrip("`")` before `stripped != line`, one whose length is compared rather than subtracted, as
    `len(line.lstrip("`")) < len(line)`, and one compared with an empty string or tested by `not`, as
    `line.strip().strip("`") == ""`, are not read."""
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        tests = any(isinstance(operator, RUN_TESTS) for operator in node.ops)
        return tests and any(_is_fence_run(operand, resolver) for operand in operands)
    if not isinstance(node, ast.Call) or not node.args:
        return False
    if _called_name(node) in AFFIX_TESTS | RUN_METHODS or resolver.name(node.func) in PATTERN_CALLS:
        return _is_fence_run(node.args[0], resolver)
    if _called_name(node) in STRIP_METHODS and _is_fence_run(node.args[0], resolver, _holds_marker):
        return _measures_stripped_run(node, resolver)
    return False


def _measures_stripped_run(node: ast.Call, resolver: CallableResolver) -> bool:
    """Whether a strip's result is compared with what it stripped, as `line.lstrip("`") != line`, or its length taken
    from that one's, as `len(line) - len(line.lstrip("`"))`, each of which reads the run it took off."""
    assert isinstance(node.func, ast.Attribute)
    stripped = ast.dump(node.func.value)
    parents = _module_parents(resolver)
    parent = parents.get(id(node))
    if isinstance(parent, ast.Compare):
        return any(ast.dump(operand) == stripped for operand in [parent.left, *parent.comparators])
    measured = parents.get(id(parent)) if isinstance(parent, ast.Call) and resolver.name(parent.func) == "len" else None
    if not (isinstance(measured, ast.BinOp) and isinstance(measured.op, ast.Sub)):
        return False
    whole = measured.left if measured.right is parent else measured.right
    return (
        isinstance(whole, ast.Call)
        and resolver.name(whole.func) == "len"
        and bool(whole.args)
        and ast.dump(whole.args[0]) == stripped
    )


def _holds_marker(value: object) -> bool:
    """Whether a literal holds a fence's marker, which str.strip takes off a line however many times it repeats."""
    return isinstance(value, str) and any(marker in value for marker in FENCE_MARKERS)


def _is_fence_run(node: ast.AST, resolver: CallableResolver, holds: Callable[[object], bool] | None = None) -> bool:
    """Whether an expression is a fence's run, or with `holds` what that test finds in a literal: a string literal
    holding one, a tuple, list, or set of literals one of which does, or a name the module binds to either."""
    holds = holds or _holds_fence_run
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_fence_run(item, resolver, holds) for item in node.elts)
    if isinstance(node, ast.Name):
        return node.id in _fence_run_names(resolver, holds)
    return isinstance(node, ast.Constant) and holds(node.value)


@functools.lru_cache(maxsize=2)
def _fence_run_names(resolver: CallableResolver, holds: Callable[[object], bool]) -> frozenset[str]:
    """The names the resolver's module binds, at any level, to what `holds` finds in a literal: a string literal
    holding it, or a tuple, list, or set of literals one of which does."""
    names: set[str] = set()
    for node in ast.walk(resolver.tree):
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = getattr(node, "value", None)
        if value is None or not targets:
            continue
        literals = value.elts if isinstance(value, (ast.Tuple, ast.List, ast.Set)) else [value]
        if any(isinstance(item, ast.Constant) and holds(item.value) for item in literals):
            names |= {target.id for target in targets if isinstance(target, ast.Name)}
    return frozenset(names)


def _holds_fence_run(value: object) -> bool:
    """Whether a literal holds a fence's run, as text, as a regular expression's repetition of a marker three or more
    times, or as a class holding a marker repeated three or more times."""
    return isinstance(value, str) and (
        any(marker * 3 in value or f"{marker}{{3" in value for marker in FENCE_MARKERS)
        or FENCE_CLASS_RUN.search(value) is not None
    )


@dataclass(frozen=True)
class SharedFact:
    """One value or function the repository keeps in one module, its owner, under one module-level name, and how
    another module restates it instead of importing it."""

    owner: str
    name: str
    restates: Restates


def _shared_facts() -> tuple[SharedFact, ...]:
    """The facts a diverged or re-derived copy was found of. The gate above compares copies by value, so a copy that
    has since drifted from its owner's, or a value re-derived from the same rule, is outside its reach; each fact here
    is read from its owner, never restated, and a second literal copy of it fails."""
    return (
        SharedFact(f"{SKILL_CORE_SCRIPTS}/git_client.py", "GLOBAL_VALUE_OPTIONS", _shares_members),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/suite_names.py", "SUITE_NAME_PATTERNS", _holds_member),
        SharedFact(
            "skills/ejryan89-code-review-core/scripts/review_config.py", "DEFAULT_CONFIG_NAME", _equals_in_importer
        ),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/agent_skills.py", "MAX_NAME", _states_number),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/agent_skills.py", "NAME_PATTERN", _derives_name_grammar),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/frontmatter.py", "USER_ONLY_KEY", _reads_key),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/typed_text.py", "TYPED_ENCODING", _reads_title_or_body),
        SharedFact("tests/validation/skill_layout.py", "HOME_SPELLINGS", _patterns_member),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/versions.py", "SEPARATOR", _reads_version_numbers),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/path_keys.py", "path_key", _folds_path_case),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/path_keys.py", "name_key", _compares_git_name),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/exit_calls.py", "EXIT_CALLS", _shares_call_names),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/markdown_sections.py", "find_fences", _reads_fence_run),
    )


def _fact_members(tree: ast.Module, name: str) -> frozenset[str] | None:
    """The strings a module-level name holds, as a string literal, a whole number written in decimal, a collection of
    string literals, or the pattern of a regular expression compiled from a literal, or the name itself when it names a
    module-level function, or None."""
    resolver = CallableResolver(tree)
    for node in module_statements(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return frozenset({name})
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return frozenset({value.value})
        if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
            return frozenset({str(value.value)})
        if (
            isinstance(value, ast.Call)
            and resolver.name(value.func) == "re.compile"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            return frozenset({value.args[0].value})
        if isinstance(value, ast.Call) and resolver.name(value.func) in COLLECTION_CALLS and len(value.args) == 1:
            value = value.args[0]
        items = _literals(value)
        return frozenset(item for item in items if isinstance(item, str)) if items else None
    return None


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """The docstrings of a module and its definitions, by node identity: prose about a fact, not a copy of it."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def shared_fact_owner_problems(root: Path) -> list[str]:
    """Report a shared fact whose owner no longer holds it as a literal or a function, so the restatement check cannot
    go quiet after a rename or a move."""
    problems: list[str] = []
    for fact in _shared_facts():
        path = root / fact.owner
        trees = list(python_trees([path] if file_presence.is_present(path) else [], root, problems))
        if not trees or _fact_members(trees[0][2], fact.name) is None:
            problems.append(
                f"{fact.owner} no longer holds {fact.name} as a literal or a function; name its new home in the shared "
                "facts of tests/validation/duplication.py"
            )
    return problems


def _restating_lines(tree: ast.Module, fact: SharedFact, members: frozenset[str]) -> list[int]:
    """The lines where the module restates the fact, docstrings aside."""
    stem = PurePath(fact.owner).stem
    imports_owner = any(stem in found.module.split(".") for found in module_imports(tree))
    prose = _docstring_nodes(tree)
    resolver = CallableResolver(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.expr)
            and id(node) not in prose
            and fact.restates(node, members, imports_owner, fact.name, resolver)
        }
    )


def _restatement_problems(
    trees: list[tuple[str, ast.Module]], allowances: dict[str, dict[str, str]], copied: dict[str, set[str]]
) -> tuple[list[str], list[str]]:
    """Each restatement of a shared fact outside its owner, and each sanction of one whose reason does not name the
    owner. A restating module sanctions the copy under the fact's name, and copied records the sanction as needed."""
    owners = dict(trees)
    found: list[str] = []
    problems: list[str] = []
    for fact in _shared_facts():
        members = _fact_members(owners[fact.owner], fact.name) if fact.owner in owners else None
        for module, tree in trees:
            lines = _restating_lines(tree, fact, members) if members and module != fact.owner else []
            if not lines:
                continue
            copied.setdefault(module, set()).add(fact.name)
            reason = allowances[module].get(fact.name)
            if reason is None:
                found += [
                    f"{module}:{line} restates {fact.name}, which {fact.owner} holds; import it from there, or "
                    f"sanction it in {module} with {DUPLICATION_ALLOWANCE}"
                    for line in lines
                ]
            elif fact.owner not in reason:
                problems.append(
                    f"{module}: {DUPLICATION_ALLOWANCE} gives a reason for {fact.name} that does not name {fact.owner}"
                )
    return found, problems


# Where a module-level statement runs: for each if or try that holds it, by node identity, the branch it is in. An if's
# body and its else are branches 0 and 1; a try's body and its else are branch 0, since the else runs only after the
# body, and each except handler is a branch of its own. A with block, and a try's finally, which runs after whichever
# branch ran, add none.
Branches = tuple[tuple[int, int], ...]


def _module_bindings(body: list[ast.stmt], branches: Branches = ()) -> Iterator[tuple[ast.stmt, Branches]]:
    """Each statement that runs at module level, the blocks module_statements reads included, with its branches."""
    for node in body:
        yield node, branches
        within = (*branches, (id(node), 0))
        blocks: list[tuple[list[ast.stmt], Branches]]
        if isinstance(node, ast.If):
            blocks = [(node.body, within), (node.orelse, (*branches, (id(node), 1)))]
        elif isinstance(node, (ast.Try, ast.TryStar)):
            handlers = [
                (handler.body, (*branches, (id(node), 1 + index))) for index, handler in enumerate(node.handlers)
            ]
            blocks = [(node.body, within), *handlers, (node.orelse, within), (node.finalbody, branches)]
        elif isinstance(node, ast.With):
            blocks = [(node.body, branches)]
        else:
            blocks = []
        for block, held in blocks:
            yield from _module_bindings(block, held)


def _exclusive(first: Branches, second: Branches) -> bool:
    """Whether two statements sit in different branches of one if or try, so one run binds at most one of them."""
    for (node, branch), (other, other_branch) in zip(first, second, strict=False):
        if node != other:
            return False
        if branch != other_branch:
            return True
    return False


def _rebound_names(name: str, tree: ast.Module) -> list[str]:
    """Each module-level name an assignment or an annotated assignment binds after another already bound it, in a
    statement at the top or in a module-level if, try, or with block, as module_statements reads them.

    The later binding silently replaces the first for every caller, as a second SENTENCE_END once replaced the
    documented one in audit-skill's inventory. Ruff's F811 reports a function or an import defined again, never an
    assignment, so this reads assignments only; an annotation without a value binds nothing. Bindings in different
    branches of one if, or in a try's body and one of its except handlers, are alternatives a run picks between, as a
    platform's value or a fallback when an import fails, so neither replaces the other and they are not reported."""
    earlier: dict[str, list[tuple[int, Branches]]] = {}
    found: list[str] = []
    for statement, branches in _module_bindings(tree.body):
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        else:
            continue
        for target in targets:
            for node in ast.walk(target):
                if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
                    continue
                bindings = earlier.setdefault(node.id, [])
                replaced = next((line for line, other in bindings if not _exclusive(other, branches)), None)
                if replaced is not None:
                    found.append(
                        f"{name}:{node.lineno} binds {node.id} again at module level after line {replaced}, "
                        "which replaces the first value for every caller; keep one binding"
                    )
                bindings.append((node.lineno, branches))
    return found


def duplicated_definition_problems(root: Path) -> list[str]:
    """Report a module-level function or class defined identically in two files, a module-level literal or compiled
    pattern assigned identically in two files, a shared fact restated outside its owner, a module-level name one file
    binds twice, and allowances no longer needed.

    Copied helpers began identical and then drifted, and so did copied patterns; a copy caught while it is still
    identical is caught before it diverges. Two scripts of one skill count, because one can import the other. A name
    bound twice in one file is a copy that has already diverged, and the second binding wins.

    Stated limit: an integer is compared only when written as a hexadecimal, octal, or binary literal, and a decimal
    integer never, since unrelated counts, limits, and versions share decimal values by chance (BIT_PATTERN_PREFIXES
    says which). A bit pattern copied in decimal, negated, or built from others, as `A | B` is, goes unseen; and since
    a bit pattern is keyed by its value alone, two unrelated ones sharing a value in different modules are refused
    as one copy and are sanctioned with DUPLICATION_ALLOWED in each.
    """
    copies: dict[str, list[Copy]] = {}
    allowances: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = [
        path
        for top in DUPLICATION_ROOTS
        for path in file_presence.matching(root / top, "**/*.py")
        if not is_test_script(path)
    ]
    ordered = sorted(files, key=lambda path: path.relative_to(root).as_posix())
    trees: list[tuple[str, ast.Module]] = []
    found: list[str] = []
    for path, name, tree in python_trees(ordered, root, problems):
        trees.append((name, tree))
        found += _rebound_names(name, tree)
        allowed = module_allowance(tree, DUPLICATION_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {DUPLICATION_ALLOWANCE} must map each name to the reason it is allowed")
            allowed = {}
        allowances[name] = allowed
        for key, place in _module_copies(name, tree, path):
            copies.setdefault(key, []).append(place)
    copied: dict[str, set[str]] = {}
    for key, group in copies.items():
        modules = sorted({module for module, _, _ in group})
        if len(modules) < 2:
            continue
        # Each module sanctions the copy under its own name for it, which a constant's copies need not share.
        held = sorted({(module, copy) for module, _, copy in group})
        for module, copy in held:
            copied.setdefault(module, set()).add(copy)
        if all(copy in allowances[module] for module, copy in held):
            # Each reason names the other copies, so a reader of one copy finds every place a change must reach.
            problems += [
                f"{module}: {DUPLICATION_ALLOWANCE} gives a reason for {copy} that does not name "
                f"every other copy: {listed(missing, 'and')}"
                for module, copy in held
                if (
                    missing := [other for other in modules if other != module and other not in allowances[module][copy]]
                )
            ]
            continue
        found.append(_copy_report(key, group))
    restated, unnamed = _restatement_problems(trees, allowances, copied)
    found += restated
    problems += unnamed
    for module, allowed in allowances.items():
        problems += [
            f"{module}: {DUPLICATION_ALLOWANCE} allows {entry}, which no other file defines identically or restates "
            "as a shared fact"
            for entry in sorted(allowed)
            if entry not in copied.get(module, set())
        ]
    return sorted(found) + sorted(problems)


# skills/ejryan89-skill-core/scripts is the one home of the repository's shared modules: skill scripts, the deployer,
# and tools/ import them from there. deployer/__init__.py puts the directory on sys.path for every deployer module,
# since a package runs its __init__.py before any of its modules; a tools module states the same path statement itself,
# above its imports, because ruff sorts an ejryan89-skill-core import above the deployer's.
SKILL_CORE_SCRIPTS = f"skills/{SKILL_CORE}/scripts"
SKILL_CORE_DOC = VALIDATION_DOC
DEPLOYER_PACKAGE = "deployer/__init__.py"


def skill_core_modules(root: Path) -> set[str]:
    """The names of ejryan89-skill-core's shared modules, which skill scripts, the deployer, and tools/ import."""
    return {path.stem for path in file_presence.matching(root / SKILL_CORE_SCRIPTS, "*.py") if not is_test_script(path)}


def skill_core_copy_problems(root: Path) -> list[str]:
    """Report a module outside ejryan89-skill-core with the name of an ejryan89-skill-core module: another copy of
    shared code.

    A renamed copy is caught by the duplication policy, which sanctions no whole module, and a same-named one here
    even after it drifts, as the frontmatter reader's deployer copy would have.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    for top in DUPLICATION_ROOTS:
        for path in file_presence.matching(root / top, "**/*.py"):
            name = path.relative_to(root).as_posix()
            if (
                path.stem in shared
                and not is_test_script(path)
                and not posix_within(name, SKILL_CORE_SCRIPTS, strict=True)
            ):
                problems.append(
                    f"{name} has the name of {SKILL_CORE_SCRIPTS}/{path.stem}.py; import {path.stem} from "
                    f"{SKILL_CORE} instead of keeping another copy; see {SKILL_CORE_DOC}"
                )
    return sorted(problems)


def _skill_core_statement_lines(tree: ast.Module) -> list[int]:
    """Lines of the module-level statements that put ejryan89-skill-core's scripts first on sys.path, located from
    __file__."""
    lines: list[int] = []
    for node in tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (
            isinstance(call, ast.Call)
            and ast.unparse(call.func) == "sys.path.insert"
            and len(call.args) == 2
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == 0
            and "__file__" in ast.unparse(call.args[1])
            and SKILL_CORE in scripts_put_on_path(node)
        ):
            lines.append(node.lineno)
    return lines


def _imported_modules(tree: ast.Module) -> list[tuple[int, str]]:
    """Each absolute import in the module, and each module it imports by name through importlib.import_module or
    __import__, as its line and the dotted module name it imports; "" for a name that is not one string literal."""
    found = {(found.line, found.module) for found in module_imports(tree)}
    resolver = CallableResolver(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and resolver.name(node.func) in MODULE_LOADERS:
            name = call_argument(node, 0, "name")
            literal = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else ""
            found.add((node.lineno, literal))
    return sorted(found)


def _unreadable_import(name: str, line: int) -> str:
    return (
        f"{name}:{line} imports a module by a name it cannot read; import it by a statement or one string literal; "
        f"see {SKILL_CORE_DOC}"
    )


def skill_core_import_problems(root: Path) -> list[str]:
    """Report a deployer or tools module that imports ejryan89-skill-core before putting it on sys.path, and an import
    that breaks the direction or the standard-library rule.

    deployer/__init__.py holds the deployer's one path statement, located from its own file and never through the
    configured source path; a tools module states it above its ejryan89-skill-core imports. `ejryan89-skill-core`
    imports nothing from the deployer or tools/, so a deployed skill never needs the checkout, and the deployer and
    ejryan89-skill-core import only the standard library, the deployer, and ejryan89-skill-core.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    package = root / DEPLOYER_PACKAGE
    package_lines = [
        line
        for _, _, tree in python_trees([package] if file_presence.is_present(package) else [], root, [])
        for line in _skill_core_statement_lines(tree)
    ]
    if not package_lines:
        problems.append(
            f"{DEPLOYER_PACKAGE} does not put {SKILL_CORE_SCRIPTS} first on sys.path, located from its own file; "
            f"see {SKILL_CORE_DOC}"
        )
    allowed = set(sys.stdlib_module_names) | shared | {"deployer"}
    for top in ("deployer", "tools", SKILL_CORE_SCRIPTS):
        for path, name, tree in python_trees(file_presence.matching(root / top, "**/*.py"), root, problems):
            statements = _skill_core_statement_lines(tree)
            if top == "deployer" and name != DEPLOYER_PACKAGE:
                problems += [
                    f"{name}:{line} puts {SKILL_CORE} on sys.path; {DEPLOYER_PACKAGE} is the deployer's one place; "
                    f"see {SKILL_CORE_DOC}"
                    for line in statements
                ]
                statements = [0] if package_lines else []
            for line, module in _imported_modules(tree):
                first = module.split(".")[0]
                if not module:
                    problems.append(_unreadable_import(name, line))
                elif top == SKILL_CORE_SCRIPTS and first in {"deployer", "tools"}:
                    problems.append(
                        f"{name}:{line} imports {module}; {SKILL_CORE} imports nothing from the deployer or tools/; "
                        f"see {SKILL_CORE_DOC}"
                    )
                elif top != "tools" and not is_test_script(path) and first not in allowed:
                    problems.append(
                        f"{name}:{line} imports {module}, which is neither the standard library, the deployer, nor "
                        f"{SKILL_CORE}; see {SKILL_CORE_DOC}"
                    )
                elif top != SKILL_CORE_SCRIPTS and first in shared and not any(at < line for at in statements):
                    problems.append(
                        f"{name}:{line} imports {module} before putting {SKILL_CORE_SCRIPTS} on sys.path; see "
                        f"{SKILL_CORE_DOC}"
                    )
    return sorted(problems)


def _module_names(scripts: Path) -> set[str]:
    return {path.stem for path in file_presence.matching(scripts, "*.py") if not is_test_script(path)}


def skill_script_import_problems(root: Path) -> list[str]:
    """Report a shipped skill script outside ejryan89-skill-core that imports a module other than the standard
    library's, ejryan89-skill-core's, its own skill's, or one of a skill its skill_deps reach, by a statement or by
    name.

    A deployed skill runs on a machine with the prerequisites and nothing else, so a module beyond these is missing
    wherever the skill is deployed, and an installed development dependency would hide that until then.
    """
    allowed = set(sys.stdlib_module_names) | skill_core_modules(root)
    problems: list[str] = []
    skills = root / "skills"
    for scripts in sorted(
        path
        for path in file_presence.matching(skills, "*/scripts")
        if file_presence.is_present(path.parent / "SKILL.md", other_is_absent=True)
    ):
        skill = scripts.parent.name
        if skill == SKILL_CORE:
            continue
        reachable = allowed | _module_names(scripts)
        for dependency in skill_dependency_closure(root, skill):
            reachable |= _module_names(skills / dependency / "scripts")
        files = sorted(path for path in file_presence.matching(scripts, "*.py") if not is_test_script(path))
        for _, name, tree in python_trees(files, root, problems):
            for line, module in _imported_modules(tree):
                if not module:
                    problems.append(_unreadable_import(name, line))
                elif module.split(".")[0] not in reachable:
                    problems.append(
                        f"{name}:{line} imports {module}, which is neither the standard library, {SKILL_CORE}, the "
                        f"skill's own scripts, nor a skill its skill_deps reach; see {SKILL_CORE_DOC}"
                    )
    return sorted(problems)


# ejryan89-skill-core's path_containment is the one test of whether a path lies in a tree, so a fix to it reaches every
# caller. Any other containment test, an is_relative_to, a commonpath or commonprefix of any path module, a search of a
# path's parents, a prefix test written by hand, a relpath or a relative_to that walks up checked for a parent segment,
# or a relative_to whose ValueError a handler or a contextlib.suppress turns into an answer, states why in a
# module-level CONTAINMENT_ALLOWED = {scope: reason}.
CONTAINMENT_ALLOWANCE = "CONTAINMENT_ALLOWED"
# The exceptions whose handler catches the ValueError relative_to raises, by the names a module reads them under.
VALUE_ERROR_CATCHERS = frozenset({"ValueError", "Exception", "BaseException"})
PATH_JOINS = frozenset(f"{module}.join" for module in PATH_MODULES)
RELATIVE_PATHS = frozenset(f"{module}.relpath" for module in PATH_MODULES)
SEPARATOR_NAMES = frozenset(f"{module}.{name}" for module in ("os", *PATH_MODULES) for name in ("sep", "altsep"))
PARENT_NAMES = frozenset(f"{module}.pardir" for module in ("os", *PATH_MODULES))
SEPARATORS = ("/", "\\")
# The string methods that test a prefix or find where one starts, and the set and sequence methods that look for a
# path among a path's parents.
PREFIX_METHODS = frozenset({"startswith", "find", "index"})
PARENT_SEARCHES = frozenset({"isdisjoint", "issubset", "issuperset", "intersection", "count", "index", "__contains__"})
# Each function that matches a text against a pattern, by the position of its pattern: a regular expression's, and a
# glob's, whose pattern may end in wildcards after the separator.
REGULAR_EXPRESSIONS = {"re.match": 0, "re.fullmatch": 0, "re.search": 0, "re.compile": 0}
GLOBS = {"fnmatch.fnmatch": 1, "fnmatch.fnmatchcase": 1, "fnmatch.filter": 1, "fnmatch.translate": 0}
# A path's own glob matches, and the keywords their pattern goes by: `pattern`, and `path_pattern` before Python 3.13.
PATH_MATCHES = frozenset({"match", "full_match"})
PATH_PATTERN_KEYWORDS = frozenset({"pattern", "path_pattern"})
# A `%` template's last field when it is a named `%(key)s` that nothing follows, and no `%%` makes it literal text.
NAMED_LAST_FIELD = re.compile(r"(?<!%)(?:%%)*%\((?P<key>[^()]*)\)s\Z")
# The builtins that make a collection of what they are given, so its items are the prefixes given.
COLLECTIONS = frozenset({"tuple", "list", "set", "frozenset", "sorted"})


def _last_item(value: ast.expr) -> ast.expr | None:
    """The last value a `%` formatting fills in: a tuple's last item, or the one value itself."""
    if isinstance(value, ast.Tuple):
        return value.elts[-1] if value.elts else None
    return value


def _last_field(template: str, positional: list[ast.expr], keywords: list[ast.keyword]) -> ast.expr | None:
    """The value a `str.format` template's last replacement field takes when nothing follows the field, by automatic
    position, explicit index, or keyword, or None: text after it, a conversion, an attribute or item of the value, a
    position an unpacking may move, or a template that does not parse."""
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        return None
    if not fields or fields[-1][1] is None or fields[-1][3] is not None:
        return None
    field = fields[-1][1]
    if field.isidentifier():
        return {keyword.arg: keyword.value for keyword in keywords}.get(field)
    index = sum(name == "" for _, name, _, _ in fields) - 1 if field == "" else whole_number.parse(field)
    if index is None or index >= len(positional):
        return None
    return None if any(isinstance(argument, ast.Starred) for argument in positional[:index]) else positional[index]


def _alternatives(value: ast.expr | None) -> list[ast.expr] | None:
    """The values an expression may give, for one that gives one of others: an assignment expression's value, either
    branch of a conditional expression, or any operand of `and` or `or`; None for any other expression."""
    if isinstance(value, ast.NamedExpr):
        return [value.value]
    if isinstance(value, ast.IfExp):
        return [value.body, value.orelse]
    return value.values if isinstance(value, ast.BoolOp) else None


def _never(_: ast.expr) -> bool:
    """What a `+=` tells of a value that only a binding can give, such as a length or a stripped text: nothing."""
    return False


def _names_parents(value: ast.expr) -> bool:
    """Whether `value` reads a path's `parents`."""
    return any(isinstance(node, ast.Attribute) and node.attr == "parents" for node in ast.walk(value))


def _returns(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.expr]:
    """Each value a function returns, leaving out those of a function or class it defines."""
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return) and node.value is not None:
            yield node.value
        if not isinstance(node, (*DEFINITIONS, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


class _ContainmentReader:
    """Reads the containment tests a module writes, following each name, attribute, and call of a function the module
    defines to every value it is given."""

    def __init__(self, tree: ast.Module) -> None:
        self.resolver = CallableResolver(tree)
        self.imported = import_aliases(tree)
        # The names being followed, by the scope that owns each, so a name bound in a cycle is followed once.
        self.following: set[tuple[int, str]] = set()
        # Trailing characters a pattern's text may end in after its separator, such as a glob's `*`.
        self.trailing = ""
        # What each name holds beyond its bindings, by its key: a parameter's default, keyed as its function's scope
        # owns it, and what an attribute is assigned and a function the module defines returns, keyed by name alone.
        self.held: dict[tuple[int, str], list[ast.expr]] = {}
        # What `+=` adds to a name or an attribute, since it binds no value of its own.
        self.added: dict[tuple[int, str], list[ast.expr]] = {}
        # What a loop or a comprehension iterates for each name it binds, whose items the name holds in turn.
        self.looped: dict[tuple[int, str], list[ast.expr]] = {}
        for node in ast.walk(tree):
            self._record(node)

    def _record(self, node: ast.AST) -> None:
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and (key := self._key(node.target)):
            self.added.setdefault(key, []).append(node.value)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension) and isinstance(node.target, ast.Name):
            self.looped.setdefault(self._owner(node.target)[0], []).append(node.iter)
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Attribute):
                    self.held.setdefault((0, f".{target.attr}"), []).append(node.value)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            arguments = node.args
            positional = [*arguments.posonlyargs, *arguments.args]
            for argument, default in [
                *zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True),
                *zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True),
            ]:
                if default is not None:
                    scope = id(self.resolver.scopes[id(argument)])
                    self.held.setdefault((scope, argument.arg), []).append(default)
            if not isinstance(node, ast.Lambda):
                self.held.setdefault((0, f"{node.name}()"), []).extend(_returns(node))

    def _key(self, node: ast.expr) -> tuple[int, str] | None:
        """The key a name or an attribute is followed by, or None for any other expression."""
        if isinstance(node, ast.Attribute):
            return 0, f".{node.attr}"
        return self._owner(node)[0] if isinstance(node, ast.Name) else None

    def _owner(self, node: ast.Name) -> tuple[tuple[int, str], list[ast.expr]]:
        """The key of the scope that owns a name, with the name, and every expression that scope binds it to; a name
        bound nowhere, such as a builtin, is owned by no scope and bound to nothing."""
        owner = self.resolver.scopes[id(node)].owner(node.id)
        if owner is None:
            return (0, node.id), []
        return (id(owner), node.id), [value for value in owner.bindings[node.id] if isinstance(value, ast.expr)]

    def _follow(self, node: ast.expr, read: Callable[[ast.expr], bool], added: Callable[[ast.expr], bool]) -> bool:
        """Whether `read` holds for any value a name, an attribute, or a call of a function the module defines is
        given, or `added` for any `+=` adds to it; False for one already being followed, so a cycle ends."""
        if isinstance(node, ast.Name):
            key, values = self._owner(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
            called = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            key, values = (0, f"{called}()"), []
        elif isinstance(node, ast.Attribute):
            key, values = (0, f".{node.attr}"), []
        else:
            return False
        if key in self.following:
            return False
        self.following.add(key)
        try:
            return (
                any(map(read, [*values, *self.held.get(key, [])]))
                or any(self._each(iterable, read) for iterable in self.looped.get(key, []))
                or any(map(added, self.added.get(key, [])))
            )
        finally:
            self.following.discard(key)

    def _each(self, iterable: ast.expr, read: Callable[[ast.expr], bool]) -> bool:
        """Whether `read` holds for an item of what a loop iterates: a literal tuple, list, or set, a comprehension's
        element, a collection a builtin such as `tuple` makes of one, or one a name, an attribute, or a function of the
        module holds."""
        if isinstance(iterable, ast.Tuple | ast.List | ast.Set):
            return any(map(read, iterable.elts))
        if isinstance(iterable, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            return read(iterable.elt)
        if isinstance(iterable, ast.Call) and self.name(iterable.func) in COLLECTIONS:
            return any(self._each(argument, read) for argument in iterable.args[:1])

        def each(held: ast.expr) -> bool:
            return self._each(held, read)

        return self._follow(iterable, each, each)

    def name(self, node: ast.AST) -> str:
        """The dotted name an expression stands for, through the names the module binds once, or else through its
        imports, which still name a module it imports in two places."""
        return self.resolver.name(node) or qualified_name(node, self.imported)

    def ends(self, value: ast.expr | None) -> bool:
        """Whether the text `value` gives ends in a separator, a literal such as "/" or "skills/" included."""
        if isinstance(value, ast.Constant):
            return isinstance(value.value, str) and value.value.rstrip(self.trailing).endswith(SEPARATORS)
        if (choices := _alternatives(value)) is not None:
            return any(map(self.ends, choices))
        if isinstance(value, ast.Name | ast.Attribute) and self.name(value) in SEPARATOR_NAMES:
            return True
        if isinstance(value, ast.Name | ast.Attribute):
            return self._follow(value, self.ends, self.ends)
        return value is not None and self.built(value)

    def built(self, value: ast.expr) -> bool:
        """Whether `value` builds a text ending in a separator from another, as a folder and its separator are built:
        by `+`, an f-string, `%` or `str.format`, a `join` of a path module or of a string, or any of these held in a
        name, an attribute, a parameter's default, or what a function of the module returns, by a binding or a `+=`,
        or in an item of a tuple or list of prefixes. A last piece of trailing characters alone, such as a glob's
        `"*"`, is passed over to the piece before it. A literal alone is no folder."""
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return self.ends(value.left if self._trails(value.right) else value.right)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mod):
            return self._interpolated(value)
        if isinstance(value, ast.JoinedStr):
            pieces = list(value.values)
            while len(pieces) > 1 and self._trails(pieces[-1]):
                pieces.pop()
            last = pieces[-1] if pieces else None
            return self.ends(last.value if isinstance(last, ast.FormattedValue) else last)
        if isinstance(value, ast.Call):
            return self._built_by_call(value)
        if isinstance(value, ast.Tuple | ast.List | ast.Set):
            return any(map(self.built, value.elts))
        if (choices := _alternatives(value)) is not None:
            return any(map(self.built, choices))
        return self._follow(value, self.built, self.ends)

    def _trails(self, piece: ast.expr) -> bool:
        """Whether a piece's static text is made of trailing characters alone, so the text ends where the piece before
        it does."""
        text = _static_text(piece, self.resolver)
        return text is not None and not text.rstrip(self.trailing)

    def _interpolated(self, value: ast.BinOp) -> bool:
        """Whether a `%` template, static as _static_text reads it, ends in a separator, or in a last field that takes
        a text ending in one: `%s` filled by the one value or a tuple's last item, or a named `%(key)s` filled from a
        mapping. A glob's trailing wildcards are passed over first, so the field before them is the last."""
        template = _static_text(value.left, self.resolver)
        if template is None:
            return False
        stripped = template.rstrip(self.trailing)
        named = NAMED_LAST_FIELD.search(stripped)
        return (
            stripped.endswith(SEPARATORS)
            or (stripped.endswith("%s") and self.ends(_last_item(value.right)))
            or (named is not None and self._mapped(value.right, named["key"]))
        )

    def _mapped(self, mapping: ast.expr, key: str) -> bool:
        """Whether a mapping gives `key` a text ending in a separator: a literal dictionary's last entry under the key,
        a `dict` call's keyword, or either held in a name, an attribute, or what a function of the module returns."""
        if isinstance(mapping, ast.Dict):
            values = [
                value
                for name, value in zip(mapping.keys, mapping.values, strict=True)
                if name is not None and _static_text(name, self.resolver) == key
            ]
            return bool(values) and self.ends(values[-1])
        if isinstance(mapping, ast.Call) and self.name(mapping.func) == "dict":
            return any(keyword.arg == key and self.ends(keyword.value) for keyword in mapping.keywords)

        def held(value: ast.expr) -> bool:
            return self._mapped(value, key)

        return self._follow(mapping, held, _never)

    def _built_by_call(self, call: ast.Call) -> bool:
        function = self.name(call.func)
        last = call.args[-1:]
        if function in PATH_JOINS:
            return any(self.ends(item) or (isinstance(item, ast.Constant) and item.value == "") for item in last)
        if function == "str.format":
            template, positional = (call.args[0] if call.args else None), call.args[1:]
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "format":
            template, positional = call.func.value, call.args
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "join":
            return any(self._joined(call.func.value, items) for items in call.args[:1])
        elif function in COLLECTIONS:
            return self._each(call, self.built)
        else:
            return self._follow(call, self.built, self.built)
        text = _static_text(template, self.resolver) if template is not None else None
        if text is None:
            return False
        # A glob's trailing wildcards are passed over first, so the field before them is the last.
        stripped = text.rstrip(self.trailing)
        return stripped.endswith(SEPARATORS) or self.ends(_last_field(stripped, positional, call.keywords))

    def _joined(self, joiner: ast.expr, items: ast.expr) -> bool:
        """Whether a join of a literal tuple or list ends in a separator: its last item does, or the joiner does and
        the last item is empty."""
        if not (isinstance(items, ast.Tuple | ast.List) and items.elts):
            return False
        last = items.elts[-1]
        empty = isinstance(last, ast.Constant) and last.value == ""
        return self.ends(last) or (empty and self.ends(joiner))

    def _pattern(self, call: ast.Call, positions: dict[str, int], trailing: str) -> bool:
        """Whether `call` matches against a pattern holding a text built to end in a separator, followed by any of
        `trailing`."""
        position = positions.get(self.name(call.func))
        pattern = call_argument(call, position, "pattern", "pat") if position is not None else None
        return pattern is not None and self._holds_folder([pattern], trailing)

    def _path_glob(self, call: ast.Call) -> bool:
        """Whether `call` is a path's `match` or `full_match`, bound or called on the class, given a pattern holding a
        text built to end in a separator, followed by any `*`. A `match` of the `re` module, or of a regular expression
        the module compiles, matches a text against a pattern, never a path."""
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in PATH_MATCHES):
            return False
        if self.name(call.func.value) == "re" or self._compiled(call.func.value):
            return False
        keywords = [keyword.value for keyword in call.keywords if keyword.arg in PATH_PATTERN_KEYWORDS]
        return self._holds_folder([*call.args, *keywords], "*")

    def _compiled(self, receiver: ast.expr) -> bool:
        """Whether `receiver` is a regular expression the module compiles: a `re.compile` call, or one held in a name,
        an attribute, or what a function of the module returns."""
        if isinstance(receiver, ast.Call) and self.name(receiver.func) == "re.compile":
            return True
        return self._follow(receiver, self._compiled, _never)

    def _holds_folder(self, patterns: list[ast.expr], trailing: str) -> bool:
        """Whether any of `patterns` holds a text built to end in a separator, followed by any of `trailing`."""
        self.trailing = trailing
        try:
            return any(
                self.built(node) for pattern in patterns for node in ast.walk(pattern) if isinstance(node, ast.expr)
            )
        finally:
            self.trailing = ""

    def _relative(self, value: ast.expr) -> bool:
        """Whether `value` holds a relpath's result, or a relative_to's that walks up with `..` segments: the call
        itself, or a name the module binds to one."""
        return any(
            (isinstance(node, ast.Call) and (self.name(node.func) in RELATIVE_PATHS or _walks_up(node)))
            or (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and self._follow(node, self._relative, self._relative)
            )
            for node in ast.walk(value)
        )

    def _parent(self, value: ast.expr) -> bool:
        """Whether `value` is a parent segment, as a literal starting with "..", os.pardir, either followed by more,
        or a tuple or list holding one."""
        if isinstance(value, ast.Constant):
            return isinstance(value.value, str) and value.value.startswith("..")
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return self._parent(value.left)
        if isinstance(value, ast.Tuple | ast.List | ast.Set):
            return any(self._parent(item) for item in value.elts)
        return self.name(value) in PARENT_NAMES

    def _length(self, value: ast.expr | None) -> bool:
        """Whether `value` is a `len` call, or a name bound to one."""
        if isinstance(value, ast.Call):
            return self.name(value.func) == "len"
        return isinstance(value, ast.Name) and self._follow(value, self._length, _never)

    def _stripped(self, value: ast.expr) -> bool:
        """Whether `value` is a text with a folder and its separator removed from its front, or a name bound to one."""
        if isinstance(value, ast.Call):
            method = value.func.attr if isinstance(value.func, ast.Attribute) else ""
            return method == "removeprefix" and any(map(self.built, value.args))
        return isinstance(value, ast.Name) and self._follow(value, self._stripped, _never)

    def _leading(self, value: ast.expr) -> bool:
        """Whether `value` slices a text or a path's parts up to the length of another."""
        return (
            isinstance(value, ast.Subscript)
            and isinstance(value.slice, ast.Slice)
            and (
                value.slice.lower is None
                or (isinstance(value.slice.lower, ast.Constant) and value.slice.lower.value == 0)
            )
            and self._length(value.slice.upper)
        )

    def _after_prefix(self, value: ast.expr) -> bool:
        """Whether `value` reads a text from the length of another on: the character there, or the rest."""
        if not isinstance(value, ast.Subscript):
            return False
        index = value.slice
        return self._length(index.lower if isinstance(index, ast.Slice) else index)

    def _holds_separator(self, value: ast.expr) -> bool:
        """Whether `value` is a separator, a literal holding one, or a tuple, list, or set holding one."""
        if isinstance(value, ast.Constant):
            return isinstance(value.value, str) and any(separator in value.value for separator in SEPARATORS)
        if isinstance(value, ast.Tuple | ast.List | ast.Set):
            return any(map(self._holds_separator, value.elts))
        return self.name(value) in SEPARATOR_NAMES

    def test(self, node: ast.AST) -> str | None:
        """The containment test `node` writes, or None: an is_relative_to, a commonpath or commonprefix of any path
        module, a search of a path's parents, by a set or sequence method, `in`, `&`, or `==` with a name a loop or a
        comprehension takes from them, a startswith, find, index, regular expression, glob, or path's `match` or
        `full_match` of a text built to end in a separator, a comparison of what a removeprefix of one leaves, a check
        for a separator after a prefix, a relpath or a relative_to that walks up checked for a parent segment, a
        comparison of a path's leading parts, or of a leading slice with a folder and its separator, or a `try` or a
        `with` whose body calls relative_to and whose handler catches, or whose `contextlib.suppress` suppresses, the
        ValueError it raises for a path outside the folder, which turns that error into an answer.

        One comparison of a parent by `==` is no search: one with the very expression whose `parents` the loop takes,
        as `(path, *path.parents)` compared with `path` to spot its first item, since a path is never among its own
        parents. A comparison with any other expression is read as containment."""
        if isinstance(node, ast.Attribute) and node.attr == "is_relative_to":
            return "is_relative_to"
        if (
            isinstance(node, ast.Try | ast.TryStar)
            and _calls_relative_to(node.body)
            and any(self._catches_value_error(handler.type) for handler in node.handlers)
        ) or (
            isinstance(node, ast.With)
            and _calls_relative_to(node.body)
            and any(self._suppresses_value_error(item.context_expr) for item in node.items)
        ):
            return "a relative_to whose ValueError is caught"
        if (
            isinstance(node, ast.Attribute | ast.Name)
            and isinstance(node.ctx, ast.Load)
            and (function := self.name(node)) in COMMON_PATH_FUNCTIONS
        ):
            return function.rpartition(".")[2]
        if isinstance(node, ast.Call):
            return self._call_test(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd) and _names_parents(node):
            return "a search of a path's parents"
        if isinstance(node, ast.Compare):
            return self._comparison_test(node)
        return None

    def _parent_taken(self, side: ast.expr, other: ast.expr) -> bool:
        """Whether a comparison's side is a name a loop or a comprehension takes from what reads a path's `parents`,
        directly or through a name the module binds to it, so comparing it by `==` with `other` searches them. A path
        is never among its own parents, so comparing with the path itself, as a walk over `(path, *path.parents)`
        does to spot its first item, searches nothing."""
        if not isinstance(side, ast.Name):
            return False
        own = ast.dump(other)

        def reads(value: ast.expr) -> bool:
            return any(
                (isinstance(node, ast.Attribute) and node.attr == "parents" and ast.dump(node.value) != own)
                or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and self._follow(node, reads, _never))
                for node in ast.walk(value)
            )

        return any(map(reads, self.looped.get(self._owner(side)[0], [])))

    def _catches_value_error(self, caught: ast.expr | None) -> bool:
        """Whether an `except` clause's type catches ValueError: bare, ValueError itself or a base of it, alone, in a
        tuple, unpacked by `*`, or held in a name the module binds to any of these."""
        if caught is None:
            return True
        if isinstance(caught, ast.Tuple):
            return any(map(self._catches_value_error, caught.elts))
        if isinstance(caught, ast.Starred):
            return self._catches_value_error(caught.value)
        if self.name(caught).removeprefix("builtins.") in VALUE_ERROR_CATCHERS:
            return True
        return isinstance(caught, ast.Name) and self._follow(caught, self._catches_value_error, _never)

    def _suppresses_value_error(self, context: ast.expr) -> bool:
        """Whether a `with` item is a `contextlib.suppress` given ValueError or a base of it."""
        return (
            isinstance(context, ast.Call)
            and self.name(context.func) == "contextlib.suppress"
            and any(self._catches_value_error(kind) for kind in context.args)
        )

    def _call_test(self, call: ast.Call) -> str | None:
        method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        subjects = [call.func.value, *call.args] if isinstance(call.func, ast.Attribute) else []
        if method in PARENT_SEARCHES and any(map(_names_parents, subjects)):
            return "a search of a path's parents"
        if method == "startswith" and any(map(self._relative, subjects)) and any(map(self._parent, call.args)):
            return "a relpath checked for a parent segment"
        if (
            method == "startswith"
            and any(map(self._after_prefix, subjects))
            and any(map(self._holds_separator, call.args))
        ):
            return "a check for a separator after a prefix"
        if method in PREFIX_METHODS and any(map(self.built, call.args)):
            return (
                "a startswith of a folder and its separator"
                if method == "startswith"
                else "a search for a folder and its separator"
            )
        if self._pattern(call, REGULAR_EXPRESSIONS, ""):
            return "a regular expression of a folder and its separator"
        if self._pattern(call, GLOBS, "*") or self._path_glob(call):
            return "a glob of a folder and its separator"
        return None

    def _comparison_test(self, comparison: ast.Compare) -> str | None:
        sides = [comparison.left, *comparison.comparators]
        searched = [
            side
            for op, side in zip(comparison.ops, comparison.comparators, strict=True)
            if isinstance(op, ast.In | ast.NotIn)
        ]
        equated = [
            pair
            for index, op in enumerate(comparison.ops)
            if isinstance(op, ast.Eq | ast.NotEq)
            for pair in ((sides[index], sides[index + 1]), (sides[index + 1], sides[index]))
        ]
        if any(map(_names_parents, searched)) or any(self._parent_taken(*pair) for pair in equated):
            return "a search of a path's parents"
        if any(map(self._parent, sides)) and any(map(self._relative, sides)):
            return "a relpath checked for a parent segment"
        if any(map(self._after_prefix, sides)) and any(map(self._holds_separator, sides)):
            return "a check for a separator after a prefix"
        if any(map(self._stripped, sides)):
            return "a removeprefix of a folder and its separator"
        leading = next(filter(self._leading, sides), None)
        if (
            isinstance(leading, ast.Subscript)
            and isinstance(leading.value, ast.Attribute)
            and leading.value.attr == "parts"
        ):
            return "a comparison of a path's leading parts"
        if leading is not None and any(map(self.built, sides)):
            return "a comparison of a leading slice"
        return None


def _calls_relative_to(statements: list[ast.stmt]) -> bool:
    """Whether `statements` call `relative_to` as they run, not inside a function, lambda, or class they define, which
    runs later, outside their handlers."""

    def calls(node: ast.AST) -> bool:
        if isinstance(node, (*DEFINITIONS, ast.Lambda)):
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "relative_to":
            return True
        return any(calls(child) for child in ast.iter_child_nodes(node))

    return any(calls(statement) for statement in statements)


def _walks_up(call: ast.Call) -> bool:
    """Whether a call is a relative_to given `walk_up` as anything but a false literal, which puts `..` segments in
    its result for a path outside the folder rather than raising."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "relative_to"
        and any(
            keyword.arg == "walk_up" and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
            for keyword in call.keywords
        )
    )


def containment_problems(root: Path, files: list[Path]) -> list[str]:
    """Each containment test outside path_containment that no CONTAINMENT_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != PATH_CONTAINMENT]
    for _, name, tree in python_trees(paths, root, problems):
        reader = _ContainmentReader(tree)
        found = [
            (
                node.lineno,
                scope,
                f"tests containment with {test}; call ejryan89-skill-core's path_containment, the one containment "
                f"test, or state why in {CONTAINMENT_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.expr | ast.Try | ast.TryStar | ast.With) and (test := reader.test(node))
        ]
        problems += sanctioned_problems(name, tree, CONTAINMENT_ALLOWANCE, found)
    return problems


class DuplicationPolicies(unittest.TestCase):
    def test_repository_copies_only_what_it_sanctions(self) -> None:
        # Copied helpers began identical and then drifted, so a copy is caught while it is still identical.
        self.assertEqual([], duplicated_definition_problems(REPOSITORY_ROOT))

    def test_repository_owners_hold_every_shared_fact(self) -> None:
        self.assertEqual([], shared_fact_owner_problems(REPOSITORY_ROOT))

    def test_repository_keeps_shared_modules_in_skill_core(self) -> None:
        self.assertEqual([], skill_core_copy_problems(REPOSITORY_ROOT))
        self.assertEqual([], skill_core_import_problems(REPOSITORY_ROOT))

    def test_shipped_skill_scripts_import_only_what_a_deployment_holds(self) -> None:
        self.assertEqual([], skill_script_import_problems(REPOSITORY_ROOT))

    def test_path_containment_is_the_one_containment_test(self) -> None:
        self.assertEqual([], containment_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_the_duplication_policy_reads_every_module_its_rule_speaks_of(self) -> None:
        modules = formatted_modules(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT))
        self.assertEqual(
            [], scope_problems("the duplication policy", modules, DUPLICATION_ROOTS, duplication_omissions())
        )

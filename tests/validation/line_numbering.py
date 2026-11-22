"""The line-numbering policy: a reader that numbers many positions of one text never counts the line breaks from the
text's start once per position, which takes time that grows with the square of the text, and never builds the text's
line starts itself, but reads them once with `text_lines.line_starts` and takes each position's line with
`text_lines.line_index`.

Once per position, inside a loop, a `for`, an `async for`, a `while`, or a comprehension, or in a function that such a
loop of the same module calls by its name, as `line_of(text, match.start())`, `self.line_of(...)`, or
`cls.line_of(...)`, in any non-test module the claim policies read, it refuses each of these forms that counts from
the text's start:

- a `count` of a line break, a literal CR, LF, or CRLF, as text or bytes, or `chr(10)` or `chr(13)`, bound or unbound
  as `str.count(text, "\\n", 0, end)`, given a literal start of 0 and an end, or given no start on a slice of the text
  from its start, as `text[:end].count("\\n")`;
- a `splitlines` of a slice of a text from its start, bound or unbound, and a `split` or `rsplit` of one at such a
  line break, by position or by `sep=`, as `len(text[:end].split("\\n"))`;
- a `text_lines`, `lines_and_breaks`, `program_lines`, or `line_starts` of text_lines given a slice of a text from its
  start, as `len(text_lines(text[:end]))`;
- a `findall`, `finditer`, or `split` of a line-break pattern given a slice of a text from its start, or a `findall`
  or `finditer` of one given a literal start of 0 and an end, as `LINE_BREAK.findall(text, 0, end)`, and `re`'s own
  `findall`, `finditer`, or `split` of a line-break pattern given a slice from the start.

A line-break pattern is text_lines' `LINE_BREAK`, `LINE_FEED`, or `KEPT_LINE`, a `re.compile` of a literal whose
pattern matches only CR, LF, and alternatives of them, as `re.compile("\\r\\n|\\r|\\n")` or `re.compile(r"\\n")`, or a
name a module-level assignment binds to one, and, for `re`'s functions, such a literal itself.

Anywhere outside text_lines.py, whether or not it runs once per position, it refuses a text's line starts built from a
line-break pattern: a comprehension, or a `for` loop, over a `finditer` of one, whose element, or whose body, takes
`.end()` of each match, as `[0, *(match.end() for match in LINE_BREAK.finditer(text))]`.

Inside a loop means in its body, in a `while` loop's test, or in a comprehension's element, conditions, or any
iterable but its first, each of which runs once per item; a comprehension's first iterable and a loop's `else` run
once. A count or a builder that must stay states why in a module-level LINE_COUNT_ALLOWED = {scope: reason}.

Stated limits, forms no module of the repository writes: a count in a function that numbers one position, called once
per position from a loop of another module, through another function, or through a name bound to it; a count in a
lambda or a function another call runs once per item, as `map(lambda p: text.count("\\n", 0, p), places)`,
`sorted(places, key=...)`, or `map(partial(line_of, text), places)`, and a method a loop calls on a receiver other
than `self` or `cls`, as `reading.line_of(place)`; a line break or a start given through a name the policy does not
resolve, as `text.count(NEWLINE, 0, end)`, and a pattern bound anywhere but at a module's top level; `re.findall` or
`re.finditer` of a line-break pattern given the whole text, which take no start, and so count only the whole text;
and a scan of each character for a line break, which no call shows. The policy also refuses, though each text is read
once, a count of the start of each of many texts, as `[text[:-1].count("\\n") for text in texts]`, and a count in any
function that shares its name with one a loop calls; LINE_COUNT_ALLOWED sanctions either.
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Iterator
from pathlib import Path

from python_checks import COMPREHENSIONS, LINE_SPLIT_OWNER, claim_files, resolves_to, sanctioned_problems
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    python_trees,
    qualified_name,
    repository_files,
    scoped_nodes,
)

LINE_COUNT_ALLOWANCE = "LINE_COUNT_ALLOWED"
# The line breaks a count may be given, as text or bytes, and the code points chr() gives them by.
LINE_BREAKS = frozenset({"\n", "\r", "\r\n", b"\n", b"\r", b"\r\n"})
LINE_BREAK_CODES = frozenset({10, 13})
# The characters a line-break pattern's text holds once its \r and \n escapes are read: CR, LF, and alternatives.
LINE_BREAK_PATTERN_CHARACTERS = frozenset("\r\n|")
# text_lines' readers of a text's lines and its line-break patterns, which a slice of a text from its start re-reads.
LINE_READERS = tuple(
    f"text_lines.{name}" for name in ("text_lines", "lines_and_breaks", "program_lines", "line_starts")
)
LINE_PATTERNS = tuple(f"text_lines.{name}" for name in ("LINE_BREAK", "LINE_FEED", "KEPT_LINE"))
# The pattern methods that take a start after the text, and those that read a slice of it.
POSITIONED_SEARCHES = frozenset({"findall", "finditer"})
PATTERN_READS = POSITIONED_SEARCHES | {"split"}
# The str and bytes methods that split a text into lines, at every break or at the one they are given.
STRING_SPLITS = frozenset({"split", "rsplit"})
REFUSAL = (
    "counts line breaks from the text's start once per position, in a loop or a function a loop calls, in time that "
    "grows with the square of the text; read the text's text_lines.line_starts once and take each position's line with "
    f"text_lines.line_index, or state why in {LINE_COUNT_ALLOWANCE}"
)
STARTS_REFUSAL = (
    "builds a text's line starts from a line-break pattern itself; take them from text_lines.line_starts, and each "
    f"position's line with text_lines.line_index, or state why in {LINE_COUNT_ALLOWANCE}"
)


def _is_zero(node: ast.expr | None) -> bool:
    """Whether an expression is the literal integer 0."""
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 0


def _from_start(node: ast.expr | None) -> bool:
    """Whether an expression slices a value from its start to a given end, as `text[:end]` or `text[0:end]` does."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
        return False
    window = node.slice
    return (window.lower is None or _is_zero(window.lower)) and window.upper is not None and window.step is None


def _is_line_break(node: ast.expr | None) -> bool:
    """Whether an expression is a literal line break, or chr() of one's code point."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str | bytes) and node.value in LINE_BREAKS
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "chr"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in LINE_BREAK_CODES
    )


def _is_line_break_pattern(node: ast.expr | None) -> bool:
    """Whether an expression is a literal regular expression that matches only line breaks: CR, LF, and their
    alternatives, written as the characters or as their escapes."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)):
        return False
    text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
    read = text.replace("\\r", "\r").replace("\\n", "\n")
    return bool(read.strip("|")) and set(read) <= LINE_BREAK_PATTERN_CHARACTERS


class _Patterns:
    """The line-break patterns one module names: text_lines' through its imports, a re.compile of a line-break literal
    in place, and the names its top level binds to such a compile."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases = import_aliases(tree)
        self.compiled: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets, value = [statement.target], statement.value
            else:
                continue
            if self._compiles(value):
                self.compiled.update(target.id for target in targets if isinstance(target, ast.Name))

    def _compiles(self, node: ast.expr) -> bool:
        """Whether an expression is a re.compile of a line-break literal."""
        return (
            isinstance(node, ast.Call)
            and resolves_to(qualified_name(node.func, self.aliases), "re.compile")
            and bool(node.args)
            and _is_line_break_pattern(node.args[0])
        )

    def named(self, node: ast.expr) -> bool:
        """Whether an expression is a line-break pattern."""
        if isinstance(node, ast.Name) and node.id in self.compiled:
            return True
        qualified = qualified_name(node, self.aliases)
        return any(resolves_to(qualified, pattern) for pattern in LINE_PATTERNS) or self._compiles(node)

    def re_function(self, call: ast.Call, names: frozenset[str]) -> bool:
        """Whether a call is one of re's functions `names` given a line-break literal as its pattern."""
        qualified = qualified_name(call.func, self.aliases)
        return (
            any(resolves_to(qualified, f"re.{name}") for name in names)
            and bool(call.args)
            and _is_line_break_pattern(call.args[0])
        )

    def finditer(self, node: ast.expr) -> bool:
        """Whether an expression is a finditer of a line-break pattern, its method or re's function."""
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "finditer" and self.named(func.value):
            return True
        return self.re_function(node, frozenset({"finditer"}))


def _string_splits_from_start(call: ast.Call) -> bool:
    """Whether a call is a str or bytes method that splits a slice of a text from its start into lines: a splitlines,
    or a split or rsplit at a line break, bound or unbound."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and (func.attr == "splitlines" or func.attr in STRING_SPLITS)):
        return False
    unbound = isinstance(func.value, ast.Name) and func.value.id in ("str", "bytes")
    receiver, given = (call.args[0], call.args[1:]) if unbound and call.args else (func.value, list(call.args))
    if not _from_start(receiver):
        return False
    if func.attr == "splitlines":
        return True
    separator = given[0] if given else next((item.value for item in call.keywords if item.arg == "sep"), None)
    return _is_line_break(separator)


def _counts_from_start(call: ast.Call, patterns: _Patterns) -> bool:
    """Whether a call counts line breaks from a text's start, in any form the module docstring names."""
    func = call.func
    first = call.args[0] if call.args else None
    if isinstance(func, ast.Attribute) and func.attr == "count":
        unbound = isinstance(func.value, ast.Name) and func.value.id in ("str", "bytes")
        receiver, given = (call.args[0], call.args[1:]) if unbound and call.args else (func.value, list(call.args))
        if not given or not _is_line_break(given[0]):
            return False
        if len(given) == 1:
            return _from_start(receiver)
        return len(given) > 2 and _is_zero(given[1])
    if any(resolves_to(qualified_name(func, patterns.aliases), reader) for reader in LINE_READERS):
        return _from_start(first)
    if patterns.re_function(call, PATTERN_READS):
        return len(call.args) > 1 and _from_start(call.args[1])
    if isinstance(func, ast.Attribute) and func.attr in PATTERN_READS and patterns.named(func.value):
        started = func.attr in POSITIONED_SEARCHES and len(call.args) > 2 and _is_zero(call.args[1])
        return _from_start(first) or started
    return _string_splits_from_start(call)


def _takes_ends(node: ast.AST, target: ast.expr) -> bool:
    """Whether a node calls `.end()` on the name a loop binds each match to."""
    if not isinstance(target, ast.Name):
        return False
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "end"
        and isinstance(inner.func.value, ast.Name)
        and inner.func.value.id == target.id
        for inner in ast.walk(node)
    )


def _builds_starts(node: ast.AST, patterns: _Patterns) -> bool:
    """Whether a comprehension or a for loop takes the end of each match of a line-break pattern's finditer."""
    if isinstance(node, COMPREHENSIONS):
        first = node.generators[0]
        elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        return patterns.finditer(first.iter) and any(_takes_ends(element, first.target) for element in elements)
    if isinstance(node, ast.For | ast.AsyncFor):
        return patterns.finditer(node.iter) and any(_takes_ends(statement, node.target) for statement in node.body)
    return False


def _looped(tree: ast.Module) -> set[int]:
    """The id of each node a loop runs once per item: a `for` loop's body, a `while` loop's test and body, and a
    comprehension's element, conditions, and every iterable but its first, with all they hold."""
    looped: set[int] = set()

    def mark(node: ast.AST, looping: bool) -> None:
        if looping:
            looped.add(id(node))
        if isinstance(node, ast.For | ast.AsyncFor):
            parts: list[tuple[ast.AST, bool]] = [(node.target, looping), (node.iter, looping)]
            parts += [(statement, True) for statement in node.body]
            parts += [(statement, looping) for statement in node.orelse]
        elif isinstance(node, ast.While):
            parts = [(node.test, True), *((statement, True) for statement in node.body)]
            parts += [(statement, looping) for statement in node.orelse]
        elif isinstance(node, COMPREHENSIONS):
            first = node.generators[0]
            parts = [(first.iter, looping), (first.target, True), *((test, True) for test in first.ifs)]
            parts += [(child, True) for child in ast.iter_child_nodes(node) if child is not first]
        else:
            parts = [(child, looping) for child in ast.iter_child_nodes(node)]
        for child, inside in parts:
            mark(child, inside)

    mark(tree, False)
    return looped


def _called_in_loops(tree: ast.Module, looped: set[int]) -> set[int]:
    """The id of each node in a function, at any depth, that a loop of the module calls by its name, as `f(...)`, or as
    a method of `self` or `cls`, as `self.f(...)`."""
    called: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and id(node) in looped):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            called.add(func.attr)
    return {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in called
        for statement in node.body
        for inner in ast.walk(statement)
    }


def _found(name: str, tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Each count from a text's start that runs once per position, and each line starts built from a line-break
    pattern outside text_lines.py, as its line, its scope, and the refusal."""
    patterns = _Patterns(tree)
    looped = _looped(tree)
    repeated = looped | _called_in_loops(tree, looped)
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.Call) and id(node) in repeated and _counts_from_start(node, patterns):
            yield node.lineno, scope, REFUSAL
        elif name != LINE_SPLIT_OWNER and _builds_starts(node, patterns):
            yield getattr(node, "lineno", 0), scope, STARTS_REFUSAL


def line_numbering_problems(root: Path, files: list[Path]) -> list[str]:
    """Each count of line breaks from a text's start once per position, and each line starts built from a line-break
    pattern outside text_lines.py, in the non-test modules the claim policies read, that no LINE_COUNT_ALLOWED entry
    sanctions, and that no module was read when none was, since a policy that read nothing found nothing."""
    read = claim_files(root, files)
    if not read:
        return ["no Python module was read, so the line-numbering policy could not judge anything"]
    problems: list[str] = []
    for _, name, tree in python_trees(read, root, problems):
        problems += sanctioned_problems(name, tree, LINE_COUNT_ALLOWANCE, list(_found(name, tree)))
    return problems


class LineNumberingPolicies(unittest.TestCase):
    def test_no_reader_counts_line_breaks_from_a_text_s_start_once_per_position(self) -> None:
        self.assertEqual([], line_numbering_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

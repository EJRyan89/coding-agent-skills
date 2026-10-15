"""MSBuild's expansion of property references and its conditions, as the dependency graph's evaluator reads them.

A property's value at a point of an evaluation is a `Value`: the values it may hold there, in the order met, `""` for
unset and None for one the reader cannot settle, such as a property an import it did not read may set, or one built
from an expression it does not read. A text is expanded, and a condition evaluated, once for each combination of the
values of the properties it names (`expanded`, `condition_value`), so a condition that reads one property twice is
decided as MSBuild decides it in each; it holds when it holds in every combination, fails when it fails in every one,
and is unsettled (None) otherwise, never guessed.

What the reader expands: `$(Name)`, with property names matched without regard to case; `$(Name.StartsWith('x'))`,
`$(Name.EndsWith('x'))`, and `$(Name.Contains('x'))`, which compare with regard to case as .NET's string methods do and
give `True` or `False`; and `$([MSBuild]::GetPathOfFileAbove('file', 'dir'))` and
`$([MSBuild]::GetDirectoryNameOfFileAbove('dir', 'file'))`, which search the folders above for a file as MSBuild
does. Any other property function, an item list `@(...)`, or metadata `%(...)` cannot be settled.

What a condition may hold: `==` and `!=`, which compare without regard to case; `<`, `>`, `<=`, and `>=` between two
numbers; `!`, `and`, `or` (the keywords in any case), and parentheses, with `or` over `and` over `!`; a quoted or a
bare operand; a lone operand read as a boolean (`true`, `on`, `yes`, `false`, `off`, `no`, each optionally after `!`);
and the functions `Exists('path')` and `HasTrailingSlash('text')`. One operand decides an `or` that holds or an `and`
that fails whatever the others are. Any other function, a malformed condition, or a lone operand that is not a
boolean is unsettled.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Any

# The values a property may hold at one point, in the order met: `""` for unset, None for one the reader cannot settle.
Value = tuple[str | None, ...]
EMPTY: Value = ("",)
UNKNOWN: Value = (None,)
# The most combinations of property values a text is expanded or a condition evaluated for, past which it is unsettled.
COMBINATION_LIMIT = 64
# Every property a text names, in either form it names one: `$(Name)` or `$(Name.Method(...))`.
PROPERTY_REFERENCE = re.compile(r"\$\(\s*([A-Za-z_][A-Za-z0-9_-]*)")
PLAIN_PROPERTY = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_-]*)\s*")
STRING_METHOD = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\.\s*(StartsWith|EndsWith|Contains)\s*\(\s*'([^']*)'\s*\)\s*", re.IGNORECASE
)
FILE_ABOVE = re.compile(
    r"\s*\[MSBuild\]::(GetPathOfFileAbove|GetDirectoryNameOfFileAbove)\s*\((?P<arguments>.*)\)\s*",
    re.IGNORECASE | re.DOTALL,
)
TRUE_WORDS = frozenset({"true", "on", "yes", "!false", "!off", "!no"})
FALSE_WORDS = frozenset({"false", "off", "no", "!true", "!on", "!yes"})
COMPARISONS = ("==", "!=", "<=", ">=", "<", ">")


def union(*values: Value) -> Value:
    """Every value any of `values` may hold, each once, in the order met."""
    return tuple(dict.fromkeys(value for each in values for value in each))


@dataclass(frozen=True)
class FileSystem:
    """What a condition or a property function may ask of the file system: whether a path exists, and the nearest
    folder at or above one that holds a file, each None when the reader does not look, as outside the examined tree.
    `directory` is the folder a relative path is taken from."""

    directory: str
    exists: Callable[[str], bool | None]
    folder_above: Callable[[str, str], str | None]


def closing(text: str, start: int) -> int | None:
    """The index of the `)` that closes the `(` at `start`, skipping quoted text, or None when none does."""
    depth = 0
    quoted = False
    for index in range(start, len(text)):
        character = text[index]
        if character == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def arguments_of(text: str) -> list[str] | None:
    """The arguments of a property function, split at the commas outside quotes and parentheses, each unquoted when it
    is quoted; None when one is not a quoted or a bare word."""
    found: list[str] = []
    depth, quoted, start = 0, False, 0
    for index, character in enumerate(f"{text},"):
        if character == "'":
            quoted = not quoted
        elif not quoted and character in "()":
            depth += 1 if character == "(" else -1
        elif not quoted and depth == 0 and character == ",":
            part = text[start:index].strip()
            found.append(part[1:-1] if len(part) > 1 and part[0] == part[-1] == "'" else part)
            start = index + 1
    return None if quoted or depth else found


def file_above(function: str, arguments: list[str], files: FileSystem) -> str | None:
    """`GetPathOfFileAbove(file, dir)` or `GetDirectoryNameOfFileAbove(dir, file)`: the file's path, or the folder
    holding it, from the nearest folder at or above the directory that holds it; `""` when none does."""
    if len(arguments) != 2:
        return None
    if function.casefold() == "getpathoffileabove":
        name, start = arguments
    else:
        start, name = arguments
    folder = files.folder_above(name, start or files.directory)
    if folder is None or folder == "":
        return folder
    return f"{folder.rstrip('/')}/{name}" if function.casefold() == "getpathoffileabove" else folder


def property_expression(inner: str, lookup: Callable[[str], str | None], files: FileSystem) -> str | None:
    """The value of one `$(...)`, `inner` being its contents, with each property it names holding one value."""
    plain = PLAIN_PROPERTY.fullmatch(inner)
    if plain is not None:
        return lookup(plain.group(1))
    method = STRING_METHOD.fullmatch(inner)
    if method is not None:
        value = lookup(method.group(1))
        if value is None:
            return None
        operation = {"startswith": value.startswith, "endswith": value.endswith, "contains": value.__contains__}
        return str(operation[method.group(2).casefold()](method.group(3)))
    function = FILE_ABOVE.fullmatch(inner)
    if function is not None:
        arguments = arguments_of(function.group("arguments"))
        expanded = [expand_one(argument, lookup, files) for argument in arguments or []]
        if arguments is None or None in expanded:
            return None
        return file_above(function.group(1), [value or "" for value in expanded], files)
    return None


def expand_one(text: str, lookup: Callable[[str], str | None], files: FileSystem) -> str | None:
    """`text` with each `$(...)` replaced by its value, each property holding one value; None when a part cannot be
    settled, such as an item list or metadata reference, which the reader does not expand."""
    parts: list[str] = []
    position = 0
    while (start := text.find("$(", position)) >= 0:
        end = closing(text, start + 1)
        if end is None:
            return None
        value = property_expression(text[start + 2 : end], lookup, files)
        if value is None:
            return None
        parts += [text[position:start], value]
        position = end + 1
    rest = text[position:]
    if "@(" in rest or "%(" in rest:
        return None
    return "".join([*parts, rest])


def combinations(names: Iterable[str], value_of: Callable[[str], Value]) -> list[dict[str, str | None]] | None:
    """Each combination of the values the named properties may hold, keyed by the name case-folded, or None when there
    are more than `COMBINATION_LIMIT`."""
    keys = list(dict.fromkeys(name.casefold() for name in names))
    choices = [value_of(key) for key in keys]
    count = 1
    for choice in choices:
        count *= len(choice)
    if count > COMBINATION_LIMIT:
        return None
    return [dict(zip(keys, chosen, strict=True)) for chosen in product(*choices)]


def lookup_in(combination: dict[str, str | None]) -> Callable[[str], str | None]:
    return lambda name: combination.get(name.casefold())


def expanded(text: str, value_of: Callable[[str], Value], files: FileSystem) -> Value:
    """The values `text` may expand to, one for each combination of the values of the properties it names."""
    found = combinations(PROPERTY_REFERENCE.findall(text), value_of)
    if found is None:
        return UNKNOWN
    return union(*((expand_one(text, lookup_in(combination), files),) for combination in found))


class Malformed(Exception):
    """A condition that is not a well-formed expression of the forms the reader knows."""


# A condition's tree: its kind first, then its parts, each a tree or an operand's text.
Node = tuple[Any, ...]


class Parser:
    """A recursive-descent reader of one condition into a tree: `or` over `and` over `!`, comparisons, function calls,
    lone operands, and parentheses."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.position = 0

    def peek(self, kind: str, text: str | None = None) -> bool:
        if self.position >= len(self.tokens):
            return False
        found_kind, found_text = self.tokens[self.position]
        return found_kind == kind and (text is None or found_text == text)

    def take(self, kind: str, text: str | None = None) -> str | None:
        if not self.peek(kind, text):
            return None
        self.position += 1
        return self.tokens[self.position - 1][1]

    def expect(self, kind: str, text: str | None = None) -> str:
        found = self.take(kind, text)
        if found is None:
            raise Malformed
        return found

    def disjunction(self) -> Node:
        parts = [self.conjunction()]
        while self.take("keyword", "or") is not None:
            parts.append(self.conjunction())
        return ("or", *parts) if len(parts) > 1 else parts[0]

    def conjunction(self) -> Node:
        parts = [self.factor()]
        while self.take("keyword", "and") is not None:
            parts.append(self.factor())
        return ("and", *parts) if len(parts) > 1 else parts[0]

    def factor(self) -> Node:
        if self.take("symbol", "!") is not None:
            return ("not", self.factor())
        if self.take("symbol", "(") is not None:
            inner = self.disjunction()
            self.expect("symbol", ")")
            return inner
        function = self.take("function")
        if function is not None:
            return ("call", function, *self.call_arguments())
        left = self.expect("string")
        for operator in COMPARISONS:
            if self.take("symbol", operator) is not None:
                return ("compare", operator, left, self.expect("string"))
        return ("boolean", left)

    def call_arguments(self) -> list[str]:
        self.expect("symbol", "(")
        found: list[str] = []
        if self.take("symbol", ")") is not None:
            return found
        while True:
            found.append(self.expect("string"))
            if self.take("symbol", ")") is not None:
                return found
            self.expect("symbol", ",")


def quoted_end(text: str, start: int) -> int | None:
    """The index of the quote that closes the one at `start`, past any `$(...)` inside, which may hold quotes."""
    position = start + 1
    while position < len(text):
        if text.startswith(("$(", "@(", "%("), position):
            end = closing(text, position + 1)
            if end is None:
                return None
            position = end + 1
        elif text[position] == "'":
            return position
        else:
            position += 1
    return None


WORD = re.compile(r"[A-Za-z0-9_.\-]+")


def tokenize(text: str) -> list[tuple[str, str]]:
    """The tokens of a condition: strings, keywords, function names, and symbols; `Malformed` on any other text."""
    found: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        character = text[position]
        if character.isspace():
            position += 1
        elif character == "'":
            end = quoted_end(text, position)
            if end is None:
                raise Malformed
            found.append(("string", text[position + 1 : end]))
            position = end + 1
        elif text.startswith(("$(", "@(", "%("), position):
            end = closing(text, position + 1)
            if end is None:
                raise Malformed
            found.append(("string", text[position : end + 1]))
            position = end + 1
        elif symbol := next(
            (item for item in (*COMPARISONS, "!", "(", ")", ",") if text.startswith(item, position)), ""
        ):
            found.append(("symbol", symbol))
            position += len(symbol)
        elif word := WORD.match(text, position):
            found.append(word_token(word.group(), text[word.end() :]))
            position = word.end()
        else:
            raise Malformed
    return found


def word_token(word: str, rest: str) -> tuple[str, str]:
    """A bare word: the keyword `and` or `or` in any case, a function's name before a parenthesis, else a string."""
    if word.casefold() in ("and", "or"):
        return ("keyword", word.casefold())
    return ("function", word) if rest.lstrip().startswith("(") else ("string", word)


@lru_cache(maxsize=4096)
def parsed(text: str) -> tuple[Node | None, tuple[str, ...]]:
    """The tree of a condition and the properties it names, or None for a malformed one."""
    try:
        parser = Parser(tokenize(text))
        tree = parser.disjunction()
    except Malformed:
        return None, ()
    if parser.position != len(parser.tokens):
        return None, ()
    return tree, tuple(PROPERTY_REFERENCE.findall(text))


def boolean(text: str | None) -> bool | None:
    """A string read as a boolean, as MSBuild reads a lone operand; None when it is not one."""
    if text is None:
        return None
    word = text.strip().casefold()
    return True if word in TRUE_WORDS else False if word in FALSE_WORDS else None


def number(text: str) -> float | None:
    try:
        return float(int(text, 16)) if text.casefold().startswith("0x") else float(text)
    except ValueError:
        return None


def compared(operator: str, left: str | None, right: str | None) -> bool | None:
    if left is None or right is None:
        return None
    if operator in ("==", "!="):
        return (left.casefold() == right.casefold()) == (operator == "==")
    first, second = number(left), number(right)
    if first is None or second is None:
        return None
    return {"<": first < second, ">": first > second, "<=": first <= second, ">=": first >= second}[operator]


def called(name: str, values: list[str | None], files: FileSystem) -> bool | None:
    value = values[0] if len(values) == 1 else None
    if value is None:
        return None
    if name.casefold() == "exists":
        return files.exists(value) if value.strip() else False
    if name.casefold() == "hastrailingslash":
        return value.endswith(("/", "\\"))
    return None


def joined(word: str, values: list[bool | None]) -> bool | None:
    """An `or` or an `and` of three-valued operands: one operand decides an `or` that holds or an `and` that fails."""
    deciding = word == "or"
    if deciding in values:
        return deciding
    return None if None in values else not deciding


def evaluated(node: Node, lookup: Callable[[str], str | None], files: FileSystem) -> bool | None:
    """The value of a condition's tree with each property holding one value; None when it cannot be settled."""
    kind = node[0]
    if kind in ("or", "and"):
        return joined(kind, [evaluated(part, lookup, files) for part in node[1:]])
    if kind == "not":
        inner = evaluated(node[1], lookup, files)
        return None if inner is None else not inner
    if kind == "compare":
        operator, left, right = (str(part) for part in node[1:])
        return compared(operator, expand_one(left, lookup, files), expand_one(right, lookup, files))
    if kind == "call":
        return called(str(node[1]), [expand_one(str(part), lookup, files) for part in node[2:]], files)
    return boolean(expand_one(str(node[1]), lookup, files))


def condition_value(text: str, value_of: Callable[[str], Value], files: FileSystem) -> bool | None:
    """Whether a condition holds: True when it holds for every combination of the values of the properties it names,
    False when it fails for every one, and None when it cannot be settled, is malformed, or is empty."""
    tree, names = parsed(text.strip())
    if tree is None:
        return None
    found = combinations(names, value_of)
    if found is None:
        return None
    results = {evaluated(tree, lookup_in(combination), files) for combination in found}
    return results.pop() if len(results) == 1 else None

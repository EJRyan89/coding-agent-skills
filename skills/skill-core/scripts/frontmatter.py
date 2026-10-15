"""Read the YAML frontmatter of a SKILL.md or agent definition, strictly.

Python's standard library has no YAML parser, and the deployer uses only the standard library, so this reads the
subset of YAML that skill and agent frontmatter uses. Values come back as text, without YAML type resolution, so
`true` is the string "true". A key's value may be:

- a plain, double-quoted, or single-quoted scalar, including one continued on indented lines;
- a literal (`|`) or folded (`>`) block scalar, with clip, strip (`-`), or keep (`+`) chomping; or
- a block sequence (`- item`) or flow sequence (`[a, "b"]`) of one-line scalars.

Reading a key whose value has any other form, such as a nested mapping, a flow mapping, an anchor, an alias, a
tag, or an explicit indentation indicator, raises FrontmatterError rather than guessing. So does a value YAML itself
reads otherwise or not at all, which a runtime's parser would refuse to load: a plain value holding `: ` or ending in
`:`, which YAML reads as a mapping, or starting with an indicator such as `- ` or `? `, and a scalar that goes on after
a comment has ended it, as a comment line at column 0 ends a block scalar. A key that is never read is never parsed,
so a skill or agent may carry hooks or other structured settings this reader does not interpret; a line indented with
a tab, which YAML forbids anywhere, is refused when the frontmatter is parsed.

This is the one reader. skill-core is the home of the repository's shared modules, so skill scripts, the deployer,
the repository tools, and the tests all import it from here as `frontmatter`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

Value = str | list[str] | None

DELIMITER = "---"
KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
NESTED_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*[ \t]*:(?:[ \t]|$)")
BLOCK_HEADER = re.compile(r"^([|>])([+-]?)$")
SEQUENCE_ITEM = re.compile(r"^-(?:[ \t]+(.*))?$")
DOUBLE_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"(?:[ \t]+#.*)?$', re.DOTALL)
SINGLE_QUOTED = re.compile(r"^'((?:[^']|'')*)'(?:[ \t]+#.*)?$", re.DOTALL)
FLOW_ITEM = re.compile(r"""[ \t]*(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^,\[\]{}"'#][^,\[\]{}]*?))[ \t]*(,|$)""")
COMMENT = re.compile(r"[ \t]+#.*$")
# Where a comment starts on a line of a plain value stripped of its indentation.
COMMENT_START = re.compile(r"(?:^|[ \t])#")
# A colon YAML reads as a mapping indicator: one followed by whitespace, or ending the value.
MAPPING_INDICATOR = re.compile(r":(?:[ \t\n]|$)")
# The indicators a plain value may not start with when whitespace or its end follows: a block sequence entry, an
# explicit mapping key, and a mapping value.
PLAIN_INDICATORS = re.compile(r"^[-?:](?:[ \t]|$)")
# What else a plain value may not start with, beside UNSUPPORTED's indicators, and why.
PLAIN_FIRST = {
    ",": "a flow indicator",
    "]": "a flow indicator",
    "}": "a flow indicator",
    "[": "a flow sequence that does not start on its key's line",
    "|": "a block scalar that does not start on its key's line",
    ">": "a block scalar that does not start on its key's line",
}
UNSUPPORTED = {
    "{": "a flow mapping",
    "&": "an anchor",
    "*": "an alias",
    "!": "a tag",
    "%": "a directive",
    "@": "a reserved indicator",
    "`": "a reserved indicator",
}


class FrontmatterError(ValueError):
    """The frontmatter, or the value of a key that was read, is not in a form this reader supports."""


def split(lines: list[str]) -> tuple[list[str], int] | None:
    """The frontmatter lines and the index of the first body line, or None when there is no frontmatter."""
    if not lines or lines[0].rstrip() != DELIMITER:
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == DELIMITER:
            return lines[1:index], index + 1
    raise FrontmatterError("frontmatter is not closed")


class Frontmatter:
    """The top-level keys of one frontmatter block; each value is parsed only when it is read."""

    def __init__(self, lines: list[str]) -> None:
        self._raw: dict[str, tuple[str, list[str]]] = {}
        self._duplicates: set[str] = set()
        current: list[str] | None = None
        for number, line in enumerate(lines, start=1):
            line = line.rstrip("\r")
            if line.startswith("\t") and line.strip():
                raise FrontmatterError(f"frontmatter line {number} is indented with a tab, which YAML forbids")
            if not line.strip() or line[0] in " \t" or line.startswith("-"):
                if current is None:
                    if line.strip():
                        raise FrontmatterError(f"frontmatter line {number} belongs to no key")
                    continue
                current.append(line)
                continue
            if line.startswith("#"):
                if current is not None:
                    current.append(line)  # it may end a scalar, which the value's reader checks
                continue
            match = KEY.match(line)
            if match is None:
                raise FrontmatterError(f"frontmatter line {number} is not a 'key: value' line")
            key = match.group(1)
            if key in self._raw:
                self._duplicates.add(key)
            current = []
            self._raw[key] = (match.group(2) or "", current)

    def __contains__(self, key: object) -> bool:
        return key in self._raw

    def keys(self) -> list[str]:
        return list(self._raw)

    def value(self, key: str) -> Value:
        """The key's value, or None when the key is absent or empty."""
        if key in self._duplicates:
            raise FrontmatterError(f"{key} appears more than once")
        if key not in self._raw:
            return None
        inline, following = self._raw[key]
        try:
            return _value(inline, following)
        except FrontmatterError as exc:
            raise FrontmatterError(f"{key}: {exc}") from None

    def string(self, key: str) -> str | None:
        """The key's value, which must not be a sequence, or None when the key is absent or empty."""
        value = self.value(key)
        if isinstance(value, list):
            raise FrontmatterError(f"{key} must be a single value, not a list")
        return value


def parse(text: str) -> Frontmatter:
    """The frontmatter of a Markdown document; an empty one when the document has none."""
    found = split(text.splitlines())
    return Frontmatter(found[0] if found else [])


def read(path: Path) -> Frontmatter:
    """The frontmatter of a UTF-8 Markdown file, which must have one."""
    found = split(path.read_text(encoding="utf-8-sig").splitlines())
    if found is None:
        raise FrontmatterError("no frontmatter")
    return Frontmatter(found[0])


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _trim(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _fold(lines: list[str]) -> str:
    """Join lines as YAML folds them: a line break becomes a space, and each empty line a newline."""
    text, blanks = "", 0
    for line in lines:
        if not line.strip():
            blanks += 1
            continue
        if text:
            text += "\n" * blanks if blanks else " "
        text += line.strip()
        blanks = 0
    return text


def _value(inline: str, following: list[str]) -> Value:
    if inline.startswith("#"):
        inline = ""
    header = COMMENT.sub("", inline)
    block = BLOCK_HEADER.match(header)
    if block:
        return _block_scalar(block.group(1), block.group(2), _before_column_comment(following))
    if inline.startswith(("|", ">")):
        raise FrontmatterError("a block scalar with an indentation indicator is not supported")
    if not inline:
        # A comment line between a sequence's items, or a nested mapping's lines, is no part of either.
        lines = [line for line in following if line.strip() and not line.strip().startswith("#")]
        if not lines:
            return None
        if all(SEQUENCE_ITEM.match(line.strip()) for line in lines):
            return _block_sequence(lines)
        if NESTED_KEY.match(lines[0].strip()) or any(SEQUENCE_ITEM.match(line.strip()) for line in lines):
            raise FrontmatterError("a nested mapping is not supported")
    content = _scalar_lines(inline, following)
    if inline.startswith("["):
        if len(content) > 1:
            raise FrontmatterError("a flow sequence must be on one line")
        return _flow_sequence(inline)
    if content[0].strip().startswith(('"', "'")):
        return _scalar(_fold(content))
    return _plain_lines(content)


def _before_column_comment(following: list[str]) -> list[str]:
    """A block scalar's lines before a comment line at column 0, which ends it; refused when lines follow that
    comment, since they then belong to no key."""
    for index, line in enumerate(following):
        if line.startswith("#"):
            if any(rest.strip() for rest in following[index + 1 :]):
                raise FrontmatterError(
                    "a comment at column 0 ends a block scalar, so the lines after it belong to no key"
                )
            return following[:index]
    return following


def _scalar_lines(inline: str, following: list[str]) -> list[str]:
    """A scalar's lines, from the first that holds it to the last before a comment line at column 0, which ends it.
    A comment line before the scalar starts is no part of it; lines after the one that ends it are refused, since
    they then belong to no key."""
    lines = [inline] if inline else []
    for index, line in enumerate(following):
        if not lines:
            if line.strip() and not line.strip().startswith("#"):
                lines.append(line)
        elif line.startswith("#"):
            if any(rest.strip() for rest in following[index + 1 :]):
                raise FrontmatterError("a comment at column 0 ends the value, so the lines after it belong to no key")
            break
        else:
            lines.append(line)
    return _trim(lines)


def _plain_lines(lines: list[str]) -> str:
    """A plain scalar on one line or continued on indented ones, folded, without the comment that ends it; refused
    when it goes on after that comment."""
    kept = []
    for index, line in enumerate(lines):
        text = line.strip()
        comment = COMMENT_START.search(text)
        if comment is None:
            kept.append(text)
            continue
        if any(rest.strip() for rest in lines[index + 1 :]):
            raise FrontmatterError("a comment ends a plain value, so the lines after it belong to no key")
        kept.append(text[: comment.start()])
        break
    return _plain(_fold(kept))


def _plain(text: str) -> str:
    """A plain scalar, refused when YAML would read it as something other than a scalar or not at all."""
    if text[:1] in UNSUPPORTED:
        raise FrontmatterError(f"{UNSUPPORTED[text[0]]} is not supported")
    if text[:1] in PLAIN_FIRST:
        raise FrontmatterError(f"a plain value starting with {text[0]!r}, {PLAIN_FIRST[text[0]]}, is not supported")
    if PLAIN_INDICATORS.match(text):
        raise FrontmatterError(f"a plain value starting with {text[0]!r} and a space is not a scalar; quote it")
    if MAPPING_INDICATOR.search(text):
        raise FrontmatterError("a plain value holding ': ' or ending in ':' is a mapping; quote it")
    return text


def _scalar(text: str) -> str:
    """A one-line plain, double-quoted, or single-quoted scalar."""
    if text[:1] in UNSUPPORTED:
        raise FrontmatterError(f"{UNSUPPORTED[text[0]]} is not supported")
    if text.startswith('"'):
        match = DOUBLE_QUOTED.match(text)
        if match is None:
            raise FrontmatterError("a double-quoted value is not closed")
        try:
            return json.loads('"' + match.group(1).replace("\t", "\\t") + '"')
        except json.JSONDecodeError:
            raise FrontmatterError("a double-quoted value uses an escape this reader does not support") from None
    if text.startswith("'"):
        match = SINGLE_QUOTED.match(text)
        if match is None:
            raise FrontmatterError("a single-quoted value is not closed")
        return match.group(1).replace("''", "'")
    return _plain(COMMENT.sub("", text).strip())


def _block_scalar(style: str, chomping: str, following: list[str]) -> str:
    content = _trim(following)
    trailing = len(following) - len(content)
    first = next((line for line in content if line.strip()), None)
    if first is None:
        return "\n" * trailing if chomping == "+" else ""
    indent = _indent(first)
    if indent == 0:
        raise FrontmatterError("a block scalar must be indented")
    body = []
    for line in content:
        if not line.strip():
            body.append("")
        elif _indent(line) < indent:
            raise FrontmatterError("a block scalar line is less indented than its first line")
        else:
            body.append(line[indent:])
    if style == ">":
        if any(line[:1] in (" ", "\t") for line in body):
            raise FrontmatterError("a folded block scalar with more-indented lines is not supported")
        text = _fold(body)
    else:
        text = "\n".join(body)
    if chomping == "-":
        return text
    if chomping == "+":
        return text + "\n" + "\n" * trailing
    return text + "\n"


def _block_sequence(lines: list[str]) -> list[str]:
    indents = {_indent(line) for line in lines}
    if len(indents) != 1:
        raise FrontmatterError("a block sequence's items must be indented alike")
    items = []
    for line in lines:
        if line[_indent(line)] == "\t":
            raise FrontmatterError("a block sequence item is indented with a tab, which YAML forbids")
        match = SEQUENCE_ITEM.match(line.strip())
        if match is None:
            raise FrontmatterError("a block sequence item must start with '-'")
        item = (match.group(1) or "").strip()
        if not item or NESTED_KEY.match(item) or item.startswith(("[", "-")):
            raise FrontmatterError("a block sequence item must be a one-line scalar")
        items.append(_scalar(item))
    return items


def _flow_sequence(text: str) -> list[str]:
    body = COMMENT.sub("", text)
    if not body.endswith("]"):
        raise FrontmatterError("a flow sequence is not closed")
    inner, position, items = body[1:-1], 0, []
    while inner[position:].strip():
        match = FLOW_ITEM.match(inner, position)
        if match is None:
            raise FrontmatterError("a flow sequence item is not a scalar")
        double, single, plain, separator = match.groups()
        if double is not None:
            items.append(_scalar(f'"{double}"'))
        elif single is not None:
            items.append(single.replace("''", "'"))
        else:
            items.append(_plain(plain.strip()))
        position = match.end()
        if not separator:
            break
    if inner[position:].strip():
        raise FrontmatterError("a flow sequence item is not a scalar")
    return items

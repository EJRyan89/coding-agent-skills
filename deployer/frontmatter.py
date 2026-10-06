"""Read the YAML frontmatter of a SKILL.md or agent definition, strictly.

Python's standard library has no YAML parser, and the deployer uses only the standard library, so this reads the
subset of YAML that skill and agent frontmatter uses. Values come back as text, without YAML type resolution, so
`true` is the string "true". A key's value may be:

- a plain, double-quoted, or single-quoted scalar, including one continued on indented lines;
- a literal (`|`) or folded (`>`) block scalar, with clip, strip (`-`), or keep (`+`) chomping; or
- a block sequence (`- item`) or flow sequence (`[a, "b"]`) of one-line scalars.

Reading a key whose value has any other form, such as a nested mapping, a flow mapping, an anchor, an alias, a
tag, or an explicit indentation indicator, raises FrontmatterError rather than guessing. A key that is never read is
never parsed, so a skill or agent may carry hooks or other structured settings this reader does not interpret.

skills/analyze-skill-cost/scripts/frontmatter.py is an identical copy, which that skill imports without the
deployer once deployed. Validation fails when the two differ, so change both together.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Definitions that tests/run_validation.py allows to be copied in another file, with the reason.
DUPLICATION_ALLOWED = {
    "*": "a deployed skill cannot import the deployer, so analyze-skill-cost ships this whole module as "
    "skills/analyze-skill-cost/scripts/frontmatter.py; validation also holds the two copies byte-identical",
}

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
            if not line.strip() or line[0] in " \t" or line.startswith("-"):
                if current is None:
                    if line.strip():
                        raise FrontmatterError(f"frontmatter line {number} belongs to no key")
                    continue
                current.append(line)
                continue
            if line.startswith("#"):
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
        return _block_scalar(block.group(1), block.group(2), following)
    if inline.startswith(("|", ">")):
        raise FrontmatterError("a block scalar with an indentation indicator is not supported")
    content = _trim(following)
    if not inline:
        lines = [line for line in content if line.strip()]
        if not lines:
            return None
        if all(SEQUENCE_ITEM.match(line.strip()) for line in lines):
            return _block_sequence(lines)
        if NESTED_KEY.match(lines[0].strip()) or any(SEQUENCE_ITEM.match(line.strip()) for line in lines):
            raise FrontmatterError("a nested mapping is not supported")
    if inline.startswith("["):
        if content:
            raise FrontmatterError("a flow sequence must be on one line")
        return _flow_sequence(inline)
    return _scalar(_fold([inline, *content]) if content else inline)


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
    return COMMENT.sub("", text).strip()


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
            items.append(plain.strip())
        position = match.end()
        if not separator:
            break
    if inner[position:].strip():
        raise FrontmatterError("a flow sequence item is not a scalar")
    return items

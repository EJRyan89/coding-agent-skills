"""The text of a Word document a pull request changes, so its reviewers read the change as they read Markdown.

A `.docx` is a zip package of XML parts, which the standard library reads. Its bytes are the pull request author's,
so the package and its XML are refused before parsing whenever they could make the reader leave the package, fetch
anything, expand without bound, or read a part twice; see `ExtractionRefused`.

Each extracted line starts with its place in the document, `[P<n>]` for the body's n-th block (a paragraph or a
whole table, every paragraph counted, empty ones included) and `[P<n> R<r>]` for a table's r-th row, so a finding on
a line points back into the document. Tracked changes stay in the line of the paragraph or row that holds them, as
`{+inserted+}` and `[-deleted-]`.
"""

from __future__ import annotations

import difflib
import io
import re
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from xml.parsers import expat

DOCUMENT_FORMATS = {".docx": "docx"}
# A Word document holds tens of parts; a package with thousands is not one.
MAX_DOCUMENT_MEMBERS = 2_000
# The most any one part is decompressed to, counted as it is read, never from the size the package declares.
MAX_DOCUMENT_PART_BYTES = 16 * 1024 * 1024
# The extracted text, as the snapshot's per-file limit for an unchanged file.
MAX_EXTRACTED_BYTES = 1024 * 1024
MAX_HEADING_CHARACTERS = 80
DIFF_CONTEXT = 3
MAIN_PART = "word/document.xml"
STYLES_PART = "word/styles.xml"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "
# Markup compatibility: a fallback repeats its choice's content for older readers, so its text would be read twice.
FALLBACK = "http://schemas.openxmlformats.org/markup-compatibility/2006 Fallback"
LABEL = re.compile(r"\[P(\d+)(?: R(\d+))?\] ")
HEADING_STYLE = re.compile(r"heading ([1-9])")
XML_ENCODING = re.compile(r"""<\?xml[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")
DECLARATION = re.compile(rb"<!(?:doctype|entity)", re.IGNORECASE)
# Every C0 control, DEL, and the characters Python's `splitlines` also breaks on: none may split or reshape a line.
LINE_UNSAFE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
EXTRACTED_NOTE = (
    "extracted: the text of this Word document, one line per paragraph or table row, each starting with its place "
    "in the document: [P<paragraph>] or [P<paragraph> R<table row>]; tracked changes show as {+inserted+} and "
    "[-deleted-]"
)
UNCHANGED_NOTE = "; no line of its text differs, so the change is to formatting, images, or parts not extracted"
NO_BASE_NOTE = "; the base version could not be extracted ({reason}), so every line shows as added"
# What reading a hostile package can raise: a damaged archive, an unsupported or corrupt compression, a cut stream.
PACKAGE_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, OSError, zlib.error)


_Start = Callable[[str, dict[str, str]], None]
_End = Callable[[str], None]
_Text = Callable[[str], None]


class ExtractionRefused(ValueError):
    """A document whose text is not extracted, with the reason, which names no part of its content."""


def document_format(path: str) -> str | None:
    """The format whose text the snapshot extracts from a changed file at `path`, by its suffix ignoring case."""
    dot = path.rfind(".")
    return None if dot == -1 or "/" in path[dot:] else DOCUMENT_FORMATS.get(path[dot:].casefold())


def extract(form: str, content: bytes) -> bytes:
    """The extracted text of a document in format `form`, as UTF-8 lines each ending in LF."""
    if form != "docx":
        raise ExtractionRefused(f"no extraction for {form}")
    lines = extract_docx(content)
    text = "".join(f"[{label}] {line}\n" for label, line in lines).encode("utf-8")
    if len(text) > MAX_EXTRACTED_BYTES:
        raise ExtractionRefused(f"its text is over {_size(MAX_EXTRACTED_BYTES)}")
    return text


def extract_docx(content: bytes) -> list[tuple[str, str]]:
    """Each block or table row of a `.docx` that holds text, as (label, text), in document order."""
    package = _open_package(content)
    with package:
        document = _read_part(package, MAIN_PART)
        if document is None:
            raise ExtractionRefused(f"the package has no {MAIN_PART}")
        styles = _read_part(package, STYLES_PART)
    # Both parts are checked before either is parsed, so a declaration in one never reaches the parser by the other.
    document = _checked(document, MAIN_PART)
    styles = None if styles is None else _checked(styles, STYLES_PART)
    headings = {} if styles is None else _heading_styles(styles)
    body = _Body(headings)
    _parse(document, MAIN_PART, body.start, body.end, body.text)
    return body.lines


def _open_package(content: bytes) -> zipfile.ZipFile:
    """The package, once it is a readable zip whose members are few, uniquely named, unencrypted, and inside it."""
    try:
        package = zipfile.ZipFile(io.BytesIO(content))
        members = package.infolist()
    except PACKAGE_ERRORS as exc:
        raise ExtractionRefused("it is not a readable zip package") from exc
    if len(members) > MAX_DOCUMENT_MEMBERS:
        raise ExtractionRefused(f"the package has more than {MAX_DOCUMENT_MEMBERS} members")
    names: set[str] = set()
    for member in members:
        if _escapes(member.orig_filename):
            raise ExtractionRefused("a member's path leaves the package")
        key = member.filename.casefold()
        if key in names:
            raise ExtractionRefused("two members share a name")
        names.add(key)
        if member.flag_bits & 0x1:
            raise ExtractionRefused("a member is encrypted")
    return package


def _escapes(name: str) -> bool:
    """Whether a member's raw name could name anything outside the package: absolute, with a drive, a backslash, a
    NUL, or an empty, `.`, or `..` segment (a folder's one trailing slash aside)."""
    if not name or name.startswith("/") or "\\" in name or "\0" in name or ":" in name:
        return True
    return any(segment in {"", ".", ".."} for segment in name.removesuffix("/").split("/"))


def _read_part(package: zipfile.ZipFile, name: str) -> bytes | None:
    """A part's bytes, read up to the limit and no further, or None when the package has no such part."""
    try:
        member = package.getinfo(name)
    except KeyError:
        return None
    try:
        with package.open(member) as stream:
            data = stream.read(MAX_DOCUMENT_PART_BYTES + 1)
    except PACKAGE_ERRORS as exc:
        raise ExtractionRefused(f"{name} cannot be read") from exc
    if len(data) > MAX_DOCUMENT_PART_BYTES:
        raise ExtractionRefused(f"{name} is over {_size(MAX_DOCUMENT_PART_BYTES)}")
    return data


def _checked(data: bytes, part: str) -> bytes:
    """A part's bytes without a byte-order mark, once they are UTF-8, declare no other encoding, and declare no
    document type and no entity, so nothing the parser could expand or fetch reaches it."""
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionRefused(f"{part} is not UTF-8") from exc
    declared = XML_ENCODING.match(data[:200].decode("utf-8", "ignore"))
    if declared and declared.group(1).casefold() not in {"utf-8", "utf8"}:
        raise ExtractionRefused(f"{part} declares an encoding other than UTF-8")
    if DECLARATION.search(data):
        raise ExtractionRefused(f"{part} declares a document type or an entity")
    return data


def _parse(data: bytes, part: str, start: _Start, end: _End, text: _Text) -> None:
    """Parse a part `_checked` passed with expat, whose own handlers refuse a document type and an entity too, and
    which never parses a parameter entity."""

    def refuse(*_: object) -> int:
        raise ExtractionRefused(f"{part} declares a document type or an entity")

    parser = expat.ParserCreate("utf-8", " ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.buffer_text = True
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ExtractionRefused(f"{part} is not well-formed XML") from exc


def _heading_styles(data: bytes) -> dict[str, int]:
    """Each paragraph style's heading level: 1 for a title, n for `heading n`, or one more than its outline level,
    through the styles it is based on."""
    named: dict[str, str] = {}
    outline: dict[str, int] = {}
    based: dict[str, str] = {}
    current: list[str | None] = [None]

    def start(name: str, attributes: dict[str, str]) -> None:
        if name == WORD + "style":
            current[0] = attributes.get(WORD + "styleId") if attributes.get(WORD + "type") == "paragraph" else None
            return
        style = current[0]
        value = attributes.get(WORD + "val", "")
        if style is None:
            return
        if name == WORD + "name":
            named[style] = value.casefold()
        elif name == WORD + "basedOn":
            based[style] = value
        elif name == WORD + "outlineLvl" and value.isdigit():
            outline[style] = int(value)

    def end(name: str) -> None:
        if name == WORD + "style":
            current[0] = None

    _parse(data, STYLES_PART, start, end, lambda _: None)
    levels: dict[str, int] = {}
    for style in {*named, *outline}:
        level = _style_level(style, named, outline, based)
        if level is not None:
            levels[style] = level
    return levels


def _style_level(style: str, named: dict[str, str], outline: dict[str, int], based: dict[str, str]) -> int | None:
    seen: set[str] = set()
    current: str | None = style
    while current is not None and current not in seen:
        seen.add(current)
        name = named.get(current, "")
        match = HEADING_STYLE.fullmatch(name)
        if match:
            return int(match.group(1))
        if name == "title":
            return 1
        if current in outline:
            return _outline_level(outline[current])
        current = based.get(current)
    return None


def _outline_level(value: int) -> int | None:
    """A heading level from an outline level, 0 for the top; 9 and above is body text."""
    return value + 1 if value < 9 else None


@dataclass
class _Block:
    """The line being collected: its label, its text, and, for a paragraph, its style, outline level, and list
    level."""

    label: str
    parts: list[str] = field(default_factory=list)
    style: str | None = None
    outline: int | None = None
    list_level: int | None = None
    cells: list[str] | None = None
    # A paragraph inside the line (in a table cell or a text box) began after text, so the next text is set off.
    pending: bool = False

    def add(self, text: str) -> None:
        if self.pending and "".join(self.parts).strip():
            self.parts.append(" / ")
        self.pending = False
        self.parts.append(text)


class _Body:
    """The expat handlers that read `word/document.xml` into labelled lines."""

    def __init__(self, headings: dict[str, int]) -> None:
        self.headings = headings
        self.lines: list[tuple[str, str]] = []
        self.stack: list[str] = []
        self.number = 0
        self.row = 0
        self.tables = 0  # tables open around the current element
        self.paragraphs = 0  # paragraphs open around the current element
        self.fallback = 0  # markup-compatibility fallbacks open around it
        self.block: _Block | None = None

    def _parent(self) -> str:
        return self.stack[-1] if self.stack else ""

    def start(self, name: str, attributes: dict[str, str]) -> None:
        parent = self._parent()
        self.stack.append(name)
        if name == FALLBACK:
            self.fallback += 1
        if self.fallback or not name.startswith(WORD):
            return
        local = name[len(WORD) :]
        if local == "tbl":
            if not self.tables and not self.paragraphs:
                self.number += 1
                self.row = 0
            self.tables += 1
        elif local == "tr" and self.tables == 1 and not self.paragraphs:
            self.row += 1
            self.block = _Block(f"P{self.number} R{self.row}", cells=[])
        elif local == "p":
            if not self.tables and not self.paragraphs:
                self.number += 1
                self.block = _Block(f"P{self.number}")
            elif self.block is not None:
                self.block.pending = True
            self.paragraphs += 1
        elif self.block is not None:
            self._mark(local, parent, attributes)

    def _mark(self, local: str, parent: str, attributes: dict[str, str]) -> None:
        """What an element inside the line adds to it: a paragraph property of a top-level paragraph, a tracked
        change's opening marker, or the text a tab, break, or hyphen stands for."""
        block = self.block
        if block is None:
            return
        value = attributes.get(WORD + "val", "")
        properties = parent == WORD + "pPr" and self.paragraphs == 1 and not self.tables
        if properties and local == "pStyle":
            block.style = value
        elif properties and local == "outlineLvl" and value.isdigit():
            block.outline = int(value)
        elif local == "ilvl" and parent == WORD + "numPr" and self.paragraphs == 1 and not self.tables:
            block.list_level = int(value) if value.isdigit() else 0
        elif local == "numPr" and parent == WORD + "pPr" and self.paragraphs == 1 and not self.tables:
            block.list_level = block.list_level or 0
        elif local == "numId" and parent == WORD + "numPr" and value == "0":
            block.list_level = None  # numbering zero takes a paragraph out of a list
        elif parent.endswith("Pr"):
            return  # a property, such as a change to a run's formatting, adds no text
        elif local in {"ins", "moveTo"}:
            block.add("{+")
        elif local in {"del", "moveFrom"}:
            block.add("[-")
        elif local in {"tab", "br", "cr"} and parent == WORD + "r":
            block.add(" ")
        elif local == "noBreakHyphen" and parent == WORD + "r":
            block.add("-")

    def end(self, name: str) -> None:
        self.stack.pop()
        if name == FALLBACK:
            self.fallback -= 1
            return
        if self.fallback or not name.startswith(WORD):
            return
        local = name[len(WORD) :]
        block = self.block
        if local == "tbl":
            self.tables -= 1
        elif local == "p":
            self.paragraphs -= 1
            if block is not None and not self.paragraphs and not self.tables:
                self._emit_paragraph(block)
        elif local == "tc" and block is not None and block.cells is not None and self.tables == 1:
            block.cells.append(_flat("".join(block.parts)))
            block.parts = []
            block.pending = False
        elif local == "tr" and block is not None and block.cells is not None and self.tables == 1:
            if any(block.cells):
                self.lines.append((block.label, "| " + " | ".join(block.cells) + " |"))
            self.block = None
        elif block is not None and local in {"ins", "moveTo"} and not self._parent().endswith("Pr"):
            block.add("+}")
        elif block is not None and local in {"del", "moveFrom"} and not self._parent().endswith("Pr"):
            block.add("-]")

    def _emit_paragraph(self, block: _Block) -> None:
        self.block = None
        text = _flat("".join(block.parts))
        if not text:
            return
        level = None if block.outline is None else _outline_level(block.outline)
        if level is None and block.style is not None:
            level = self.headings.get(block.style)
        if level is not None:
            text = "#" * min(level, 6) + " " + text
        elif block.list_level is not None:
            text = "  " * min(block.list_level, 8) + "- " + text
        self.lines.append((block.label, text))

    def text(self, data: str) -> None:
        if self.fallback or self.block is None or not self.stack:
            return
        if self.stack[-1] in {WORD + "t", WORD + "delText"}:
            self.block.add(data)


def _flat(text: str) -> str:
    """One line: no character that breaks or reshapes it, no run of spaces, no empty tracked-change marker."""
    text = " ".join(LINE_UNSAFE.sub(" ", text).split())
    previous = None
    while previous != text:
        previous = text
        text = text.replace("{++}", "").replace("[--]", "").replace("{+ +}", " ").replace("[- -]", " ")
    return " ".join(text.split())


def split_label(line: str) -> tuple[str, str]:
    """An extracted line's label and its text, or an empty label for a line that has none."""
    match = LABEL.match(line)
    return ("", line) if match is None else (line[1 : match.end() - 2], line[match.end() :])


def locations(text: str) -> list[str]:
    """Each extracted line's place in the source document, for a report: its paragraph, its table row, and the
    nearest heading above it."""
    places: list[str] = []
    heading = ""
    for line in text.splitlines():
        match = LABEL.match(line)
        if match is None:
            places.append("")
            continue
        place = f"paragraph {match.group(1)}" + (f", table row {match.group(2)}" if match.group(2) else "")
        body = line[match.end() :]
        if match.group(2) is None and body.startswith("#"):
            heading = body.lstrip("#").strip()
            if len(heading) > MAX_HEADING_CHARACTERS:
                heading = heading[: MAX_HEADING_CHARACTERS - 3].rstrip() + "..."
            places.append(place)
            continue
        places.append(place + (f', under the heading "{heading}"' if heading else ""))
    return places


def quoted_path(prefix: str, path: str) -> str:
    """`prefix` and `path` as git writes them in a diff header: quoted, with each byte outside printable ASCII and
    each quote and backslash escaped, when any is there."""
    raw = (prefix + path).encode("utf-8")
    if all(_plain(byte) for byte in raw):
        return prefix + path
    return '"' + "".join(chr(byte) if _plain(byte) else _escaped(byte) for byte in raw) + '"'


def _plain(byte: int) -> bool:
    return 0x20 <= byte < 0x7F and byte not in b'"\\'


def _escaped(byte: int) -> str:
    return "\\" + chr(byte) if byte in b'"\\' else f"\\{byte:03o}"


def document_diff(
    header: Sequence[str], old: str | None, new: str | None, base: str, head: str, *, base_refused: str | None = None
) -> str:
    """A file's diff block, from its `diff --git` header lines, its old and new paths (None for a side the change has
    no file on), and the extracted text of each side.

    Lines are matched on their text without their labels, so a paragraph inserted early shows as one added line, not
    as every later line renumbered; each line is then shown with its own side's label, so the new side's lines are
    exactly the head file's, and a hunk's line numbers are that file's.
    """
    base_lines, head_lines = base.splitlines(), head.splitlines()
    note = EXTRACTED_NOTE
    if base_refused is not None:
        note += NO_BASE_NOTE.format(reason=base_refused)
        base_lines = []
    lines = [*header, note]
    lines.append("--- " + ("/dev/null" if old is None else quoted_path("a/", old)))
    lines.append("+++ " + ("/dev/null" if new is None else quoted_path("b/", new)))
    matcher = difflib.SequenceMatcher(
        None, [split_label(line)[1] for line in base_lines], [split_label(line)[1] for line in head_lines]
    )
    hunks = 0
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        hunks += 1
        lines.append(f"@@ -{_range(group[0][1], group[-1][2])} +{_range(group[0][3], group[-1][4])} @@")
        for tag, first, last, start, stop in group:
            if tag == "equal":
                lines.extend(" " + line for line in head_lines[start:stop])
                continue
            lines.extend("-" + line for line in base_lines[first:last])
            lines.extend("+" + line for line in head_lines[start:stop])
    if not hunks:
        lines[len(header)] += UNCHANGED_NOTE
        del lines[len(header) + 1 :]
    return "\n".join(lines) + "\n"


def _range(start: int, stop: int) -> str:
    """A hunk's range as unified diff writes it: the first line, from 1, and the count when it is not 1; an empty
    range names the line before it."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f"{start + 1 if length else start},{length}"


def _size(limit: int) -> str:
    """A limit as a reason names it: whole mebibytes, else bytes."""
    mebibyte = 1024 * 1024
    return f"{limit // mebibyte} MiB" if limit % mebibyte == 0 else f"{limit:,} bytes"

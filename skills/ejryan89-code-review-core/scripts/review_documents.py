"""The text of a Word document or PDF a pull request changes, so its reviewers read the change as they read Markdown.

A `.docx` is a zip package of XML parts, which the standard library reads. Its bytes are the pull request author's,
so the package and its XML are refused before parsing whenever they could make the reader leave the package, fetch
anything, expand without bound, or read a part twice; see `ExtractionRefused`.

Each extracted line starts with its place in the document, `[P<n>]` for the body's n-th block (a paragraph or a
whole table, every paragraph counted, empty ones included) and `[P<n> R<r>]` for a table's r-th row, so a finding on
a line points back into the document. Tracked changes stay in the line of the paragraph or row that holds them, as
`{+inserted+}` and `[-deleted-]`.

The standard library cannot read a PDF's text, so a `.pdf` is read by `pdftotext`, an optional tool: absent, the
document is refused with a reason that names it, so the review is incomplete rather than silently partial. It is given
the bytes as a throwaway file under the system temporary directory, no stdin, and a time limit, and its output is
read no further than the extraction limit, and asked to keep the text in the order the PDF draws it, so the two
families that ship it, Xpdf and Poppler, each read one line per line of text. Their lines can still differ where one
line's pieces are drawn apart, such as a raised suffix, which Poppler may put on a line of its own. Before writing
document bytes, the shared provider policy probes, with no input, each `pdftotext` ejryan89-skill-core's locator finds,
in its search order, and takes the first it accepts, the one `deploy.py check` reports, then extraction uses that
checked path; when it accepts none, the refusal names each one it passed over and leaves a gap. This is not sandboxing.
Each line starts with `[p<page> l<line>]`, the line counted among the page's lines that hold text.
"""

from __future__ import annotations

import difflib
import io
import lzma
import re
import subprocess
import sys
import tempfile
import zipfile
import zlib
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import whole_number
from bounded_process import streaming
from executable_locator import find_executables
from flat_text import flat_text
from pdf_tool import PdfToolRefused, install_text, probe_pdftotext, select_pdftotext

DOCUMENT_FORMATS = {".docx": "docx", ".pdf": "pdf"}
# A PDF is extracted whether or not the snapshot's NUL test finds it binary: one written without compressed streams
# holds no NUL byte, and its bytes are page-description operators, not the text a reader sees.
EXTRACTED_WHEN_TEXT = frozenset({"pdf"})
PDFTOTEXT = "pdftotext"
# From starting pdftotext to its last byte of output, for one document.
PDF_EXTRACTION_SECONDS = 60.0
# How long a pdftotext stopped at the extraction limit gets to exit by itself before it is killed.
PDF_EXIT_SECONDS = 5.0
# A Word document holds tens of parts; a package with thousands is not one.
MAX_DOCUMENT_MEMBERS = 2_000
# The most any one part is decompressed to, counted as it is read, never from the size the package declares.
MAX_DOCUMENT_PART_BYTES = 16 * 1024 * 1024
# The extracted text, as the snapshot's per-file limit for an unchanged file.
MAX_EXTRACTED_BYTES = 1024 * 1024
MAX_HEADING_CHARACTERS = 80
DIFF_CONTEXT = 3
# The most steps the shortest edit search takes between a diff's anchored ends before difflib matches those lines
# instead. It takes about three million a second, so this bounds it near two-thirds of a second, twice what a
# 2,000-row table of identical rows with every third row changed needs.
MATCH_STEPS = 2_000_000
MAIN_PART = "word/document.xml"
STYLES_PART = "word/styles.xml"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "
# Markup compatibility: a fallback repeats its choice's content for older readers, so its text would be read twice.
FALLBACK = "http://schemas.openxmlformats.org/markup-compatibility/2006 Fallback"
LABEL = re.compile(r"\[(?:P(\d+)(?: R(\d+))?|p(\d+) l(\d+))\] ")
HEADING_STYLE = re.compile(r"heading ([1-9])")
# A heading's line as `_Body` writes it: a `#` for each level, then one space. Body text that would start with `#` is
# escaped with a backslash, as Markdown escapes it, so this shape is a heading's alone.
HEADING_MARKER = re.compile(r"#{1,6} ")
HASH_FIRST = re.compile(r"\\*#")
XML_ENCODING = re.compile(r"""<\?xml[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")
DECLARATION = re.compile(rb"<!(?:doctype|entity)", re.IGNORECASE)
EXTRACTED_NOTES = {
    "docx": (
        "extracted: the text of this Word document, one line per paragraph or table row, each starting with its "
        "place in the document: [P<paragraph>] or [P<paragraph> R<table row>]; tracked changes show as {{+inserted+}} "
        "and [-deleted-]"
    ),
    "pdf": (
        "extracted: the text of this PDF as pdftotext reads it, one line per line of text in the order the PDF draws "
        "it, each starting with its place in the document: [p<page> l<line>], counting the page's lines that hold "
        "text; the snapshot holds it at {held}, since a file named .pdf is read as a PDF"
    ),
}
UNCHANGED_NOTE = "; no line of its text differs, so the change is to formatting, images, or parts not extracted"
NO_BASE_NOTE = "; the base version could not be extracted ({reason}), so every line shows as added"
# What reading a hostile package can raise: a damaged archive, an unsupported or corrupt compression, a cut stream.
PACKAGE_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    NotImplementedError,
    EOFError,
    OSError,
    zlib.error,
    lzma.LZMAError,
)


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
    """The extracted text of a document in format `form`, as UTF-8 lines each ending in LF.

    Every failure to extract it is an `ExtractionRefused`, so each caller refuses that one document and goes on. A
    failure the readers below do not name is refused by its type alone, since its message may quote the document.
    """
    try:
        lines = _extracted_lines(form, content)
    except ExtractionRefused:
        raise
    except Exception as exc:
        raise ExtractionRefused(f"its text could not be read ({type(exc).__name__})") from exc
    text = "".join(f"[{label}] {line}\n" for label, line in lines).encode("utf-8")
    if len(text) > MAX_EXTRACTED_BYTES:
        raise ExtractionRefused(f"its text is over {_size(MAX_EXTRACTED_BYTES)}")
    return text


def _extracted_lines(form: str, content: bytes) -> list[tuple[str, str]]:
    if form == "docx":
        return extract_docx(content)
    if form == "pdf":
        return extract_pdf(content)
    raise ExtractionRefused(f"no extraction for {form}")


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


def extract_pdf(content: bytes) -> list[tuple[str, str]]:
    """Each line of a PDF's text that holds any, as (label, text), page by page, as `pdftotext` reads it."""
    try:
        text = _pdftotext(content).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionRefused(f"{PDFTOTEXT} wrote text that is not UTF-8") from exc
    lines: list[tuple[str, str]] = []
    # pdftotext ends every page with a form feed, so the piece after the last one holds no text.
    for page, body in enumerate(text.split("\f"), 1):
        number = 0
        for raw in body.split("\n"):
            line = flat_text(raw)
            if line:
                number += 1
                lines.append((f"p{page} l{number}", line))
    return lines


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "_pdftotext": "runs the pdftotext ejryan89-skill-core's pdf_tool chose among the programs "
    "find_executables(PDFTOTEXT) "
    "found, and the policy reads the program at that lookup",
}


def _pdftotext(content: bytes) -> bytes:
    """What `pdftotext` writes for a PDF's bytes, read no further than the extraction limit.

    The bytes go to a throwaway file in a folder of their own under the system temporary directory, which is removed
    afterwards. Its messages are off, and `-raw` keeps the text in the order the PDF draws it, which both families
    accept: without it, Xpdf joins a paragraph's evenly set lines into one where Poppler keeps one line per line of
    text, so a finding's line would depend on which of them is first on PATH.
    """
    found = find_executables(PDFTOTEXT)
    if not found:
        raise ExtractionRefused(f"{PDFTOTEXT}, which extracts a PDF's text, is not installed; {install_text()}")
    try:
        executable = select_pdftotext(found, probe_pdftotext).path
    except PdfToolRefused as exc:
        raise ExtractionRefused(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="code-review-pdf-", ignore_cleanup_errors=True) as folder:
        document = Path(folder) / "document.pdf"
        document.write_bytes(content)
        command = [executable, "-q", "-enc", "UTF-8", "-eol", "unix", "-raw", str(document), "-"]
        try:
            with streaming(
                command, idle_timeout=PDF_EXTRACTION_SECONDS, timeout=PDF_EXTRACTION_SECONDS, exit_wait=PDF_EXIT_SECONDS
            ) as running:
                running.stdin.close()
                output = running.read(MAX_EXTRACTED_BYTES + 1)
                if len(output) > MAX_EXTRACTED_BYTES:
                    raise ExtractionRefused(f"its text is over {_size(MAX_EXTRACTED_BYTES)}")
                status = running.wait()
        except subprocess.TimeoutExpired as exc:
            raise ExtractionRefused(f"{PDFTOTEXT} did not finish within {PDF_EXTRACTION_SECONDS:g} seconds") from exc
        except OSError as exc:
            raise ExtractionRefused(f"{PDFTOTEXT} could not be started") from exc
    if status != 0:
        raise ExtractionRefused(f"{PDFTOTEXT} could not read it (exit status {status})")
    return output


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
        elif name == WORD + "outlineLvl" and (level := _number(value)) is not None:
            outline[style] = level

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


INT_PARSE_ALLOWED = {
    "_style_level": "the pattern takes one digit from 1 to 9",
}


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


def _number(value: str) -> int | None:
    """An attribute's whole number in ASCII digits, as Word writes it, or None: `str.isdigit` also passes a
    superscript digit, which `int` refuses, and `int` refuses digits past the interpreter's limit."""
    return whole_number.parse(value)


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
        elif properties and local == "outlineLvl" and (level := _number(value)) is not None:
            block.outline = level
        elif local == "ilvl" and parent == WORD + "numPr" and self.paragraphs == 1 and not self.tables:
            block.list_level = _number(value) or 0
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
        elif HASH_FIRST.match(text):
            text = "\\" + text
        self.lines.append((block.label, text))

    def text(self, data: str) -> None:
        if self.fallback or self.block is None or not self.stack:
            return
        if self.stack[-1] in {WORD + "t", WORD + "delText"}:
            self.block.add(data)


def _flat(text: str) -> str:
    """One line of a Word document: as `flat_text` makes it, with no empty tracked-change marker."""
    text = flat_text(text)
    previous = None
    while previous != text:
        previous = text
        text = text.replace("{++}", "").replace("[--]", "").replace("{+ +}", " ").replace("[- -]", " ")
    return flat_text(text)


def split_label(line: str) -> tuple[str, str]:
    """An extracted line's label and its text, or an empty label for a line that has none."""
    match = LABEL.match(line)
    return ("", line) if match is None else (line[1 : match.end() - 2], line[match.end() :])


def unlabeled(text: str) -> list[str]:
    """An extracted text's lines without their labels, as `document_diff` matches them: two texts whose unlabeled
    lines are equal give a diff with no hunk."""
    return [split_label(line)[1] for line in text.splitlines()]


def locations(text: str) -> list[str]:
    """Each extracted line's place in the source document, for a report: its paragraph, its table row, and the
    nearest heading above it, or a PDF's page and line."""
    places: list[str] = []
    heading = ""
    for line in text.splitlines():
        match = LABEL.match(line)
        if match is None:
            places.append("")
            continue
        if match.group(3) is not None:
            places.append(f"page {match.group(3)}, line {match.group(4)}")
            continue
        place = f"paragraph {match.group(1)}" + (f", table row {match.group(2)}" if match.group(2) else "")
        body = line[match.end() :]
        marker = HEADING_MARKER.match(body) if match.group(2) is None else None
        if marker is not None:
            heading = body[marker.end() :]
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
    header: Sequence[str],
    old: str | None,
    new: str | None,
    base: str,
    head: str,
    *,
    base_refused: str | None = None,
    form: str = "docx",
    held: str = "",
) -> str:
    """A file's diff block, from its `diff --git` header lines, its old and new paths (None for a side the change has
    no file on), and the extracted text of each side, in format `form`; `held` names where the snapshot holds the
    head text when that is not the document's own path.

    Lines are matched on their text without their labels, so a paragraph inserted early shows as one added line, not
    as every later line renumbered; each line is then shown with its own side's label, so the new side's lines are
    exactly the head file's, and a hunk's line numbers are that file's.
    """
    base_lines, head_lines = base.splitlines(), head.splitlines()
    note = EXTRACTED_NOTES[form].format(held=held)
    if base_refused is not None:
        note += NO_BASE_NOTE.format(reason=base_refused)
        base_lines = []
    lines = [*header, note]
    lines.append("--- " + ("/dev/null" if old is None else quoted_path("a/", old)))
    lines.append("+++ " + ("/dev/null" if new is None else quoted_path("b/", new)))
    matcher = _AnchoredMatcher(
        [split_label(line)[1] for line in base_lines], [split_label(line)[1] for line in head_lines]
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


class _AnchoredMatcher(difflib.SequenceMatcher[str]):
    """A line matcher that matches the longest common start and end of two texts, then the fewest lines that differ
    between them, so `get_grouped_opcodes` writes the hunks from matches a reviewer can trust.

    difflib alone is not enough for documents, whose paragraphs and table rows repeat. It pairs the longest stretch of
    identical lines first, wherever that falls, so one paragraph inserted among identical ones shows as every line on
    one side of it removed and added again, and above 200 lines its popular-line heuristic leaves a repeated line
    unmatched altogether, so two changes around a long run show the whole run as changed. Without the heuristic it
    takes time that grows with the cube of a repeated run, minutes for a two-thousand-row table. So the anchored ends
    settle one change anywhere, and what lies between them is matched by a shortest edit search (`_shortest_edit`),
    whose time grows with the number of lines that differ rather than with the length of a repeated run; only a pair
    that needs more than `MATCH_STEPS` of it is matched by difflib, as before.
    """

    def __init__(self, base: list[str], head: list[str]) -> None:
        super().__init__(None, base, head)
        self.base, self.head = base, head

    def get_matching_blocks(self) -> list[difflib.Match]:
        base, head = self.base, self.head
        shorter = min(len(base), len(head))
        start = 0
        while start < shorter and base[start] == head[start]:
            start += 1
        end = 0
        while end < shorter - start and base[-1 - end] == head[-1 - end]:
            end += 1
        old, new = base[start : len(base) - end], head[start : len(head) - end]
        middle = _shortest_edit(old, new, MATCH_STEPS)
        if middle is None:
            middle = [
                (first, second, size)
                for first, second, size in difflib.SequenceMatcher(None, old, new).get_matching_blocks()[:-1]
            ]
        blocks = [difflib.Match(0, 0, start)] if start else []
        blocks.extend(difflib.Match(first + start, second + start, size) for first, second, size in middle)
        if end:
            blocks.append(difflib.Match(len(base) - end, len(head) - end, end))
        blocks.append(difflib.Match(len(base), len(head), 0))
        return blocks


def _shortest_edit(base: list[str], head: list[str], budget: int) -> list[tuple[int, int, int]] | None:
    """The matching blocks of a shortest edit turning `base` into `head`, as `(base, head, size)` triples, or None when
    finding it takes more than `budget` steps, each a diagonal tried or a pair of lines compared.

    This is Myers's greedy search: for each count d of lines that differ, the furthest point each diagonal of the edit
    graph reaches, so it takes about d squared steps and one more for each line matched, however long a run of
    identical lines is. Two lengths that differ by n need at least n(n+1)/2 steps, so a pair for which that passes the
    budget is given up on at once. The matches it finds are then moved within runs of identical lines (`_paired`).
    """
    if not base or not head:
        return []
    apart = abs(len(base) - len(head))
    if apart * (apart + 1) // 2 > budget:
        return None
    numbers: dict[str, int] = {}
    old = [numbers.setdefault(line, len(numbers)) for line in base]
    new = [numbers.setdefault(line, len(numbers)) for line in head]
    width, height = len(old), len(new)
    middle = width + height + 1
    reach = [0] * (2 * middle + 1)
    trace: list[array[int]] = []
    steps = 0
    for edits in range(width + height + 1):
        for diagonal in range(-edits, edits + 1, 2):
            if diagonal == -edits or (
                diagonal != edits and reach[middle + diagonal - 1] < reach[middle + diagonal + 1]
            ):
                x = reach[middle + diagonal + 1]
            else:
                x = reach[middle + diagonal - 1] + 1
            y = start = x - diagonal
            while x < width and y < height and old[x] == new[y]:
                x += 1
                y += 1
            steps += 1 + y - start
            reach[middle + diagonal] = x
            if x >= width and y >= height:
                return _paired(old, new, _backtrack(trace, width, height))
        trace.append(array("q", reach[middle - edits : middle + edits + 1 : 2]))
        if steps > budget:
            return None
    raise AssertionError("a shortest edit is at most every line of both sides")


def _backtrack(trace: list[array[int]], width: int, height: int) -> list[tuple[int, int, int]]:
    """The matching blocks of the path `_shortest_edit` found to (`width`, `height`), from `trace`, whose d-th entry
    holds the furthest point each diagonal from -d to d reached with d lines differing."""
    blocks: list[tuple[int, int, int]] = []
    x, y = width, height
    for edits in range(len(trace), 0, -1):
        reached = trace[edits - 1]
        diagonal = x - y
        if diagonal == -edits or (
            diagonal != edits and reached[(diagonal + edits - 2) // 2] < reached[(diagonal + edits) // 2]
        ):
            before = reached[(diagonal + edits) // 2]
            start, previous = before, (before, before - diagonal - 1)
        else:
            before = reached[(diagonal + edits - 2) // 2]
            start, previous = before + 1, (before, before - diagonal + 1)
        if x > start:
            blocks.append((start, start - diagonal, x - start))
        x, y = previous
    if x:
        blocks.append((0, 0, x))
    return blocks[::-1]


def _paired(old: list[int], new: list[int], blocks: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """`blocks` with as many lines matched, moved within runs of identical lines so that a line one side drops sits,
    where it can, beside a line the other side adds.

    A shortest edit leaves open which of a run's identical lines is the one dropped, and the search picks one far from
    the change: a changed row in a table of identical rows comes out as a row added there and an identical row dropped
    elsewhere, as short an edit as that row removed and added but labelled at the wrong rows. Swapping a dropped line
    with a matched one of the same text keeps the matched text, and so the edit, unchanged.
    """
    kept_old, kept_new = bytearray(len(old)), bytearray(len(new))
    for first, second, size in blocks:
        kept_old[first : first + size] = b"\x01" * size
        kept_new[second : second + size] = b"\x01" * size
    _slide(old, kept_old, kept_new)
    _slide(new, kept_new, kept_old)
    paired: list[tuple[int, int, int]] = []
    for first, second in zip(
        (index for index, kept in enumerate(kept_old) if kept),
        (index for index, kept in enumerate(kept_new) if kept),
        strict=True,
    ):
        if paired and paired[-1][0] + paired[-1][2] == first and paired[-1][1] + paired[-1][2] == second:
            paired[-1] = (paired[-1][0], paired[-1][1], paired[-1][2] + 1)
        else:
            paired.append((first, second, 1))
    return paired


def _slide(lines: list[int], kept: bytearray, other: bytearray) -> None:
    """Moves the lines of `lines` that `kept` leaves unmatched, within each run of identical lines, into the gaps
    between matched pairs where `other` leaves lines of the other side unmatched, one for each."""
    mine, theirs = [0] * (sum(kept) + 1), [0] * (sum(kept) + 1)
    for flags, counts in ((kept, mine), (other, theirs)):
        gap = 0
        for flag in flags:
            gap += flag
            counts[gap] += not flag
    gap = start = 0
    while start < len(lines):
        stop = start + 1
        while stop < len(lines) and lines[stop] == lines[start]:
            stop += 1
        matched = sum(kept[start:stop])
        if 0 < matched < stop - start:
            _spread(kept, start, stop, gap, mine, theirs)
        gap += matched
        start = stop


def _spread(kept: bytearray, start: int, stop: int, first: int, mine: list[int], theirs: list[int]) -> None:
    """Rearranges the run of identical lines from `start` to `stop`, whose first gap is `first`: each unmatched line
    stays in a gap that needs it, else moves to the first gap that does, else stays, and `mine` counts it there."""
    gaps: list[int] = []
    gap = first
    for flag in kept[start:stop]:
        gap += flag
        if not flag:
            gaps.append(gap)
            mine[gap] -= 1
    last = gap
    placed = [0] * (last - first + 1)
    spare: list[int] = []
    for gap in gaps:
        if mine[gap] < theirs[gap]:
            mine[gap] += 1
            placed[gap - first] += 1
        else:
            spare.append(gap)
    target = first
    for gap in spare:
        while target <= last and mine[target] >= theirs[target]:
            target += 1
        chosen = target if target <= last else gap
        mine[chosen] += 1
        placed[chosen - first] += 1
    index = start
    for slot, count in enumerate(placed):
        kept[index : index + count] = bytes(count)
        index += count
        if slot < last - first:
            kept[index] = 1
            index += 1


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

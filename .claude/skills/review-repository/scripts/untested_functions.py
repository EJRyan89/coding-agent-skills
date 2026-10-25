"""The untested-changes route's condition: list the functions, methods, and classes a pull request adds or changes
that no test names, so the reviewer asks for the test each one needs.

    python -B untested_functions.py --source-root <snapshot>

A review runs it as a specialists manifest's condition. Its environment names the pull request's unified diff in
CODE_REVIEW_DIFF_FILE and the file to write in CODE_REVIEW_CONDITION_OUTPUT. It reads each changed Python, PowerShell,
or Bash file at the head, except a suite and anything under tests/fixtures/, which holds review inputs rather than
this repository's code. Its definitions are Python's module-level functions and classes (those in a module-level
`if`, `try`, or `with` block included) and each class's methods, read through `ast`, leaving out a class that only
declares fields or constants, as a dataclass, an enumeration, or an exception does, and each PowerShell `function`
and Bash `name()` or `function name`, spanning to the brace that closes it. A definition changed when a line the diff
adds sits inside it, or lines it removes sat inside it and held no definition of their own, so a change inside a
method is the method's, not its class's; a removed definition is gone and needs no test, and a renamed one is the
new name, changed.

A test is a suite by validation's rule, at the head: a file under tests/ (outside tests/fixtures/) or a skill's
scripts/ whose name or stem matches SUITE_NAMES in conditions.py, with a SCRIPT_SUFFIXES extension, so a test the
pull request deletes names nothing. A test names a Python definition only through the module it imports:
`module.name`, or `from module import name`; a method as an attribute in a test that names its class, and a dunder
method by naming its class.
A string or a comment never counts, so neither a patch target nor a note does, and neither does `run` in
`subprocess.run`, nor a common name in a test of another module. A Python test `ast` or `tokenize` cannot read names
nothing, its comments included, and gets an `UNREADABLE <test>: <reason>` line on stdout. A script's `main` also
counts as named when a test names the script's file name outside a comment, since that test runs it. A test names a
PowerShell or Bash function when it names the script's file name and, outside a comment, the function as a whole word
(a PowerShell one ignoring case), since a test calls a shell function from a string.

It writes each unnamed definition as `UNTESTED <file>:<line> <kind> <name>, added lines <lines>, hunk <header>, tests
of its file <tests>`, the tests that import its module or name its file, where a reviewer looks for one that reaches
it through a caller, followed by that hunk, cut at MAX_HUNK_CHARACTERS; at most MAX_ENTRIES of them, with an
`OMITTED <n>` line for the rest; and `UNPARSED <file>: <reason>` for a changed Python file `ast` cannot read.

It reads the diff through conditions.read_files, so a changed file whose path could name a file outside the snapshot
is neither read nor listed, and gets a `REFUSED <path>: ...` line on stdout instead.

A changed script the snapshot leaves out by an exclusion its manifest records, as it leaves out every path under
`.claude/` as `agent-instruction`, cannot be read, so it gets an `EXCLUDED <file>: the snapshot leaves it out
(<reason>), so no definition in it was read; review its hunks from the diff, hunks <headers>` entry followed by its
first hunk, cut at MAX_HUNK_CHARACTERS, which opens the route for the reviewer to judge it from the diff.

A snapshot it cannot read its evidence from, one that is not a directory, whose manifest cannot be read, or that lacks
a changed script the diff leaves at the head with no exclusion recorded for it, ends it with `FAILED MissingEvidence:
<what is missing>`, so an absent snapshot never reads as every changed definition tested.

Prints `UNTESTED <found> given=<n>`. Exit status 0 means it wrote entries, which opens the route; 1 that every
changed definition is named by a test, or none changed, which closes it; 2 a usage error or a last line
FAILED <reason>, which fails the review.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

from conditions import (
    DIFF_VARIABLE,
    FAILURE_STATUS,
    OUTPUT_VARIABLE,
    SCRIPT_SUFFIXES,
    MissingEvidence,
    is_suite,
    read_files,
    refusal_line,
    require_snapshot,
    run_condition,
    snapshot_exclusions,
    unset_variables_line,
)
from console import failed_line, run_main
from diff_paths import HUNK_HEADER
from suite_names import SUITE_NAME_PATTERNS

DUPLICATION_ALLOWED = {
    "EXIT_CONTRACT_EXEMPT": (
        ".claude/skills/review-repository/scripts/doc_passages.py speaks the same condition protocol and states "
        "the same exemption; validation reads an exemption from the script it excuses, so neither imports it"
    ),
}

EXIT_CONTRACT_EXEMPT = (
    "a code-review condition: exit 0 opens its route and 1 closes it, so a failure exits 2, which fails the review "
    "rather than silently closing the route"
)
# Where validation looks for suites: tests/, and each shipped skill's and each repository skill's scripts/.
SUITE_ROOTS = ("tests", "skills/**/scripts", ".claude/skills/*/scripts")
# The route's `reads` in specialists.json: each file under a suite root whose name a suite's may be, with any
# extension, since a glob cannot test a stem, so a suite-name pattern that ends in its stem takes any extension; the
# suite rule then picks the suites from what the snapshot holds.
SUITE_READ_NAMES = tuple(pattern if pattern.endswith("*") else f"{pattern}.*" for pattern in SUITE_NAME_PATTERNS)
# Review inputs: their trees are other repositories, and validation runs no suite there.
FIXTURES = "tests/fixtures/"
# A reviewer asks for a test for each entry in turn; past this many, the change is one to review whole.
MAX_ENTRIES = 20
# Each entry's hunk is cut to this many characters, so the file stays under the CONDITION_FILE_BYTES a review gives.
MAX_HUNK_CHARACTERS = 900
# An entry names at most this many runs of the lines it adds; any one of them is where its finding sits.
MAX_LINE_RANGES = 10
# The tests of an entry's file it names, where the reviewer looks for one that reaches the entry through a caller.
MAX_RELATED_TESTS = 3

PYTHON_HEADER = re.compile(r"^\s*(?:async\s+def|def|class)\s+[A-Za-z_]")
POWERSHELL_HEADER = re.compile(r"^\s*function\s+(?:(?:global|script|local|private):)?([A-Za-z_][\w-]*)", re.IGNORECASE)
BASH_HEADER = re.compile(r"^\s*(?:function\s+([A-Za-z_][\w-]*)(?:\s*\(\s*\))?|([A-Za-z_][\w-]*)\s*\(\s*\))\s*(?:\{|$)")
COMMENT_LINE = re.compile(r"^\s*#")
POWERSHELL_BLOCK_COMMENT = re.compile(r"<#.*?#>", re.DOTALL)


@dataclass(frozen=True)
class Hunk:
    header: str
    text: str


@dataclass(frozen=True)
class Removal:
    """Lines a hunk removes: `before` is the head line they preceded, so they sat between it and the one above."""

    before: int
    lines: tuple[str, ...]
    hunk: Hunk


@dataclass
class FileChange:
    path: str
    head: Path  # where the snapshot holds it
    hunks: list[Hunk] = field(default_factory=list)  # in diff order
    added: dict[int, Hunk] = field(default_factory=dict)  # each non-blank added head line, and its hunk
    removals: list[Removal] = field(default_factory=list)


@dataclass(frozen=True)
class Definition:
    path: str
    name: str
    kind: str  # "function", "method", or "class"
    line: int  # its `def`, `class`, or `function` line
    start: int  # its first line, a decorator's included
    end: int  # its last line
    tail: int  # its last line and the blank lines after it
    column: int
    owner: str | None = None  # a method's class


INT_PARSE_ALLOWED = {
    "_hunks": "git writes a hunk header's numbers, and a diff prefixes each line of a file's text",
}


def _hunks(lines: list[str]) -> Iterator[tuple[int, list[str]]]:
    """Each hunk of one file's part of a diff: its first head line and its lines, the header first."""
    start: int | None = None
    first = 0
    for index, line in enumerate(lines):
        if match := HUNK_HEADER.match(line):
            if start is not None:
                yield first, lines[start:index]
            start, first = index, int(match.group(1))
    if start is not None:
        yield first, lines[start:]


def read_diff(text: str, root: Path) -> tuple[list[FileChange], list[str]]:
    """Each file the diff changes and leaves at the head, in diff order, with the lines it adds and the lines it
    removes, read through conditions.read_files, and each path that refused."""
    changes: list[FileChange] = []
    files, refused = read_files(text, root)
    for file in files:
        if file.new is None or file.head is None or not file.lines:
            continue
        change = FileChange(file.new, file.head)
        for first, hunk_lines in _hunks(list(file.lines)):
            _read_hunk(change, first, hunk_lines)
        changes.append(change)
    return changes, refused


def _read_hunk(change: FileChange, line_number: int, lines: list[str]) -> None:
    """Record a hunk's added lines and removed blocks; `line_number` is its first head line."""
    hunk = Hunk(lines[0].split(" @@", 1)[0] + " @@", "\n".join(lines).rstrip("\n"))
    change.hunks.append(hunk)
    removed: list[str] = []
    for line in lines[1:]:
        marker, body = line[:1], line[1:]
        if marker == "-":
            removed.append(body)
            continue
        if marker not in {"+", " "}:
            continue  # "\ No newline at end of file", or the blank line that ends the diff
        if removed:
            change.removals.append(Removal(line_number, tuple(removed), hunk))
            removed = []
        if marker == "+" and body.strip():
            change.added[line_number] = hunk
        line_number += 1
    if removed:
        change.removals.append(Removal(line_number, tuple(removed), hunk))


def _blank_after(lines: list[str], end: int) -> int:
    while end < len(lines) and not lines[end].strip():
        end += 1
    return end


def _python_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists a module-level `if`, `try`, or `with` holds, whose definitions are module-level too."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.Try | ast.TryStar):
        return [node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody]
    if isinstance(node, ast.With | ast.AsyncWith):
        return [node.body]
    return []


def _declares_only(node: ast.ClassDef) -> bool:
    """Whether a class only declares, holding fields, constants, and a docstring but no behavior of its own to test,
    as a dataclass, an enumeration, or an exception does."""
    return all(isinstance(item, ast.AnnAssign | ast.Assign | ast.Expr | ast.Pass) for item in node.body)


def python_definitions(path: str, text: str) -> list[Definition]:
    """A Python file's module-level functions and classes and each such class's methods. Raises SyntaxError."""
    lines = text.split("\n")
    found: list[Definition] = []

    def visit(body: list[ast.stmt], owner: str | None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef) and _declares_only(node):
                continue
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                kind = "class" if isinstance(node, ast.ClassDef) else "method" if owner else "function"
                start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
                end = node.end_lineno or node.lineno
                tail = _blank_after(lines, end)
                found.append(Definition(path, node.name, kind, node.lineno, start, end, tail, node.col_offset, owner))
                if isinstance(node, ast.ClassDef) and owner is None:
                    visit(node.body, node.name)
            elif owner is None:
                for block in _python_blocks(node):
                    visit(block, None)

    visit(ast.parse(text).body, None)
    return found


def _shell_header(path: str, line: str) -> str | None:
    if path.endswith(".ps1"):
        match = POWERSHELL_HEADER.match(line)
        return match.group(1) if match else None
    match = BASH_HEADER.match(line)
    return (match.group(1) or match.group(2)) if match else None


def shell_definitions(path: str, text: str) -> list[Definition]:
    """A PowerShell or Bash file's functions, each to the brace that closes it, or to the end of the file."""
    lines = text.split("\n")
    found: list[Definition] = []
    for index, line in enumerate(lines):
        name = _shell_header(path, line)
        if name is None or COMMENT_LINE.match(line):
            continue
        depth, opened, end = 0, False, len(lines)
        for offset, body in enumerate(lines[index:]):
            depth += body.count("{") - body.count("}")
            opened = opened or "{" in body
            if opened and depth <= 0:
                end = index + offset + 1
                break
        column = len(line) - len(line.lstrip())
        found.append(Definition(path, name, "function", index + 1, index + 1, end, end, column))
    return found


def _is_header(path: str, line: str) -> bool:
    return bool(PYTHON_HEADER.match(line)) if path.endswith(".py") else _shell_header(path, line) is not None


def _indent(lines: Iterable[str]) -> int:
    first = next((line for line in lines if line.strip()), "")
    return len(first) - len(first.lstrip())


def _innermost(candidates: Iterable[Definition]) -> Definition | None:
    return max(candidates, key=lambda definition: definition.start, default=None)


@dataclass
class Changed:
    """A changed definition's added head lines, and the hunk that first changes it."""

    added: list[int]
    hunk: Hunk


def changed_definitions(change: FileChange, definitions: list[Definition]) -> dict[Definition, Changed]:
    """Each definition the change adds a line to or removes lines from inside, with the head lines it adds there."""
    changed: dict[Definition, Changed] = {}
    for line in sorted(change.added):
        inside = _innermost(item for item in definitions if item.start <= line <= item.end)
        if inside is not None:
            changed.setdefault(inside, Changed([], change.added[line])).added.append(line)
    for removal in change.removals:
        if not any(text.strip() for text in removal.lines):
            continue
        if any(_is_header(change.path, text) for text in removal.lines):
            continue  # a definition removed, which needs no test, or one moved, whose new place is an added line
        above, below, indent = removal.before - 1, removal.before, _indent(removal.lines)
        # Inside means between two of its lines, or after its last one and indented as its body is.
        inside = _innermost(
            item
            for item in definitions
            if item.start <= above and (below <= item.end or (above <= item.tail and indent > item.column))
        )
        if inside is not None:
            changed.setdefault(inside, Changed([], removal.hunk))
    return changed


@dataclass
class PythonTest:
    """What one Python test names in code: modules by the names it binds them to, names it imports from each, the
    attribute it reads through each dotted name, and every attribute it reads."""

    aliases: dict[str, set[str]] = field(default_factory=dict)  # a dotted name -> the modules it may be
    imported: dict[str, set[str]] = field(default_factory=dict)  # a module -> the names imported from it
    reads: set[tuple[str, str]] = field(default_factory=set)  # (dotted name, attribute)
    attributes: set[str] = field(default_factory=set)

    def modules(self) -> set[str]:
        return {module for modules in self.aliases.values() for module in modules} | set(self.imported)

    def names(self, module: str, name: str) -> bool:
        """Whether it names `module.name`, through an alias of the module or an import of the name."""
        if name in self.imported.get(module, set()):
            return True
        return any(module in self.aliases.get(dotted, set()) for dotted, attribute in self.reads if attribute == name)


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def read_python_test(text: str) -> PythonTest:
    """The names a Python test uses in code. Raises SyntaxError or ValueError when `ast` cannot read it."""
    test = PythonTest()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                test.aliases.setdefault(bound, set()).add(alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module.rsplit(".", 1)[-1]
            for alias in node.names:
                test.imported.setdefault(module, set()).add(alias.name)
                # `from package import module` binds a module, which the test then reads attributes through.
                test.aliases.setdefault(alias.asname or alias.name, set()).add(alias.name)
        elif isinstance(node, ast.Attribute):
            test.attributes.add(node.attr)
            dotted = _dotted(node.value)
            if dotted:
                test.reads.add((dotted, node.attr))
    return test


def _code_text(path: str, text: str) -> str:
    """A test's text without its comments: a Python test's strings stay, since a test calls a shell function from
    one. Raises tokenize.TokenError or SyntaxError when `tokenize` cannot read a Python test."""
    if path.endswith(".py"):
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return " ".join(token.string for token in tokens if token.type != tokenize.COMMENT)
    if path.endswith(".ps1"):
        text = POWERSHELL_BLOCK_COMMENT.sub(" ", text)
    return "\n".join(line for line in text.split("\n") if not COMMENT_LINE.match(line))


def file_name(script: str) -> re.Pattern[str]:
    """A script's file name as a path's last segment, ignoring case: `tool.py` names neither `mytool.py` nor
    `tool.pyc`."""
    return re.compile(rf"(?<![\w.-]){re.escape(script)}(?![\w-])", re.IGNORECASE)


@dataclass
class Tests:
    """The suites the snapshot holds, read when a definition first needs them. A Python test `ast` or `tokenize`
    cannot read names nothing, and `unreadable` holds why, by its path."""

    files: dict[str, str]  # path -> text
    unreadable: dict[str, str] = field(default_factory=dict)  # path -> the reason it could not be read
    _python: dict[str, PythonTest] = field(default_factory=dict)
    _code: dict[str, str] = field(default_factory=dict)

    def python(self, path: str) -> PythonTest:
        if path not in self._python:
            try:
                self._python[path] = read_python_test(self.files[path])
            except (SyntaxError, ValueError) as error:
                self._unreadable(path, error)
                self._python[path] = PythonTest()
        return self._python[path]

    def code(self, path: str) -> str:
        if path not in self._code:
            try:
                self._code[path] = _code_text(path, self.files[path])
            except (tokenize.TokenError, SyntaxError, ValueError) as error:
                self._unreadable(path, error)
                self._code[path] = ""
        return self._code[path]

    def _unreadable(self, path: str, error: Exception) -> None:
        self.unreadable.setdefault(path, failed_line(error).removeprefix("FAILED "))

    def names(self, definition: Definition) -> bool:
        if definition.path.endswith(".py"):
            return self._names_python(definition)
        return self._names_shell(definition)

    def imports(self, path: str, module: str) -> bool:
        return path.endswith(".py") and module in self.files[path] and module in self.python(path).modules()

    def runs(self, script: str) -> bool:
        """Whether a test names a script's file name outside a comment, which is how a test runs it."""
        name = file_name(script)
        return any(name.search(self.files[path]) and name.search(self.code(path)) for path in self.files)

    def of_file(self, definition: Definition) -> list[str]:
        """The tests that import the definition's module or name its file, those named after the file first, then
        those beside it, at most MAX_RELATED_TESTS: where a reviewer looks for one that reaches it through a caller."""
        folder, _, script = definition.path.rpartition("/")
        module = script.removesuffix(".py") if script.endswith(".py") else ""
        name = file_name(script)
        found = [
            path for path in self.files if name.search(self.files[path]) or (module and self.imports(path, module))
        ]
        stem = script.rpartition(".")[0].casefold()
        found.sort(key=lambda path: (stem not in path.rpartition("/")[2].casefold(), path.rpartition("/")[0] != folder))
        return found[:MAX_RELATED_TESTS]

    def _names_python(self, definition: Definition) -> bool:
        script = definition.path.rpartition("/")[2]
        module = script.removesuffix(".py")
        if definition.kind == "function" and definition.name == "main" and self.runs(script):
            return True
        for path in self.files:
            if not self.imports(path, module):
                continue
            test = self.python(path)
            if definition.owner is None:
                if test.names(module, definition.name):
                    return True
            elif test.names(module, definition.owner) and (
                definition.name.startswith("__") or definition.name in test.attributes
            ):
                return True
        return False

    def _names_shell(self, definition: Definition) -> bool:
        script = definition.path.rsplit("/", 1)[-1]
        flags = re.IGNORECASE if definition.path.endswith(".ps1") else 0
        word = re.compile(rf"(?<![\w-]){re.escape(definition.name)}(?![\w-])", flags)
        name = file_name(script)
        return any(name.search(text) and word.search(self.code(path)) for path, text in self.files.items())


def suites(root: Path) -> dict[str, str]:
    """Each suite the snapshot holds, by its repository path, and its text."""
    found: dict[str, str] = {}
    for suite_root in SUITE_ROOTS:
        for path in root.glob(f"{suite_root}/**/*"):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(FIXTURES) or relative in found or not is_suite(relative) or not path.is_file():
                continue
            found[relative] = path.read_bytes().decode("utf-8", errors="replace")
    return dict(sorted(found.items()))


def _definitions(path: str, text: str) -> list[Definition]:
    return python_definitions(path, text) if path.endswith(".py") else shell_definitions(path, text)


def _line_ranges(lines: list[int]) -> str:
    if not lines:
        return "none (it only removes lines)"
    ranges: list[list[int]] = []
    for line in lines:
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    shown = ",".join(str(first) if first == last else f"{first}-{last}" for first, last in ranges[:MAX_LINE_RANGES])
    return shown + (",…" if len(ranges) > MAX_LINE_RANGES else "")


def _excerpt(text: str) -> str:
    return text if len(text) <= MAX_HUNK_CHARACTERS else text[:MAX_HUNK_CHARACTERS] + "…"


def excluded_entry(change: FileChange, reason: str) -> str:
    """The block for a changed script the snapshot leaves out for `reason`, which the reviewer judges from the diff."""
    headers = ", ".join(hunk.header for hunk in change.hunks)
    return (
        f"EXCLUDED {change.path}: the snapshot leaves it out ({reason}), so no definition in it was read; review its "
        f"hunks from the diff, hunks {headers}\n{_excerpt(change.hunks[0].text)}\n"
    )


def entries(root: Path, files: list[FileChange], unreadable: dict[str, str] | None = None) -> list[str]:
    """One block for each changed script the snapshot leaves out by a recorded exclusion, then for each changed
    definition no test names, and for each changed Python file `ast` cannot read; `unreadable` gains each test it
    consulted and could not read, with the reason."""
    changes = [
        change
        for change in files
        if change.path.endswith(SCRIPT_SUFFIXES) and not change.path.startswith(FIXTURES) and not is_suite(change.path)
    ]
    if not changes:
        return []
    excluded = snapshot_exclusions(root)
    withheld = [change for change in changes if not change.head.is_file() and change.path in excluded]
    absent = [change.path for change in changes if not change.head.is_file() and change.path not in excluded]
    if absent:
        raise MissingEvidence(f"the snapshot holds no {json.dumps(absent[0])}, which the diff leaves at the head")
    blocks = [excluded_entry(change, excluded[change.path]) for change in withheld]
    changes = [change for change in changes if change not in withheld]
    tests = Tests(suites(root), {} if unreadable is None else unreadable)
    for change in changes:
        text = change.head.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
        try:
            definitions = _definitions(change.path, text)
        except (SyntaxError, ValueError) as error:
            blocks.append(f"UNPARSED {change.path}: {failed_line(error).removeprefix('FAILED ')}\n")
            continue
        changed = changed_definitions(change, definitions)
        for definition in sorted(changed, key=lambda item: item.line):
            if tests.names(definition):
                continue
            found, related = changed[definition], tests.of_file(definition)
            blocks.append(
                f"UNTESTED {change.path}:{definition.line} {definition.kind} "
                f"{f'{definition.owner}.' if definition.owner else ''}{definition.name}, added lines "
                f"{_line_ranges(found.added)}, hunk {found.hunk.header}, tests of its file "
                f"{', '.join(related) or 'none'}\n{_excerpt(found.hunk.text)}\n"
            )
    return blocks


def render(blocks: list[str]) -> str:
    """The file the route's reviewer reads: each entry given, then how many past the bound were not."""
    given = list(blocks[:MAX_ENTRIES])
    if len(blocks) > MAX_ENTRIES:
        given.append(f"OMITTED {len(blocks) - MAX_ENTRIES} more entries past the first {MAX_ENTRIES}\n")
    return "\n".join(given)


def _run() -> int:
    parser = argparse.ArgumentParser(description="List the changed functions no test names.")
    parser.add_argument("--source-root", required=True, type=Path)
    arguments = parser.parse_args()
    unset = unset_variables_line()
    if unset:
        print(unset)
        return FAILURE_STATUS
    require_snapshot(arguments.source_root)
    diff = Path(os.environ[DIFF_VARIABLE]).read_bytes().decode("utf-8", errors="replace")
    changes, refused = read_diff(diff, arguments.source_root)
    for path in refused:
        print(refusal_line(path))
    unreadable: dict[str, str] = {}
    found = entries(arguments.source_root, changes, unreadable)
    for path, reason in sorted(unreadable.items()):
        print(f"UNREADABLE {json.dumps(path)}: {reason}")
    print(f"UNTESTED {len(found)} given={min(len(found), MAX_ENTRIES)}")
    if not found:
        return 1
    Path(os.environ[OUTPUT_VARIABLE]).write_bytes(render(found).encode("utf-8"))
    return 0


def main() -> int:
    return run_condition(_run)


if __name__ == "__main__":
    run_main(main, newline="\n")

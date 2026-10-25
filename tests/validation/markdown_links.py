"""Documents that point elsewhere keep pointing at something: relative Markdown links and their fragments, the
tests a document cites, the mechanism and guard each pattern of docs/lessons.md names, and the security-sensitive
area SECURITY.md gives each boundary of docs/design.md's trust model.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from duplication import SKILL_CORE_SCRIPTS
from validation_support import (
    COMMONMARK_CODE_SPAN,
    OPERATIONS_CONTRACT,
    REPOSITORY_ROOT,
    fence_holders,
    listed,
    markdown_section,
    python_trees,
    repository_files,
)

MARKDOWN_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
MARKDOWN_LINK = re.compile(r"\]\(\s*(?:<([^>\n]+)>|([^\s()<>]+))")
MARKDOWN_REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?:<([^>\n]+)>|(\S+))")
URL_SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")
# GitHub's heading slug keeps letters, digits, underscores, hyphens, and spaces, then turns each space into a hyphen.
SLUG_REMOVED = re.compile(r"[^\w\- ]")


def _outside_fences(text: str) -> list[str]:
    """The Markdown's lines, with fenced code blocks blanked so their text is never read as a link or heading."""
    lines = text.splitlines()
    return ["" if holder is not None else line for line, holder in zip(lines, fence_holders(lines), strict=True)]


def heading_slugs(text: str) -> set[str]:
    """The fragment GitHub gives each heading, numbering a repeated one -1, -2, and so on."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for line in _outside_fences(text):
        heading = MARKDOWN_HEADING.match(line)
        if heading:
            slug = SLUG_REMOVED.sub("", heading.group(1).strip().lower()).replace(" ", "-")
            slugs.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
            seen[slug] = seen.get(slug, 0) + 1
    return slugs


def markdown_link_problems(root: Path) -> list[str]:
    """Report each relative link in the repository's Markdown to a missing file, or to a heading its target lacks.

    Code blocks and spans are not links; URLs with a scheme are not checked. Files Git ignores, and the session
    worktrees under .claude/worktrees/, are not read.
    """
    from urllib.parse import unquote

    problems: list[str] = []
    for path in sorted(repository_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.suffix.casefold() != ".md" or name.startswith(".claude/worktrees/"):
            continue
        for number, line in enumerate(_outside_fences(path.read_text(encoding="utf-8")), start=1):
            # A span may hold a link's brackets, and one written with two backticks a backtick, so neither is a link.
            text = COMMONMARK_CODE_SPAN.sub("", line)
            for match in [*MARKDOWN_LINK.finditer(text), *MARKDOWN_REFERENCE.finditer(text)]:
                target = match.group(1) or match.group(2)
                if URL_SCHEME.match(target):
                    continue
                location, _, fragment = target.partition("#")
                destination = path.parent / unquote(location) if location else path
                if not destination.exists():
                    problems.append(f"{name}:{number} links to {target}, which does not exist")
                elif (
                    fragment
                    and destination.is_file()
                    and destination.suffix.casefold() == ".md"
                    and unquote(fragment) not in heading_slugs(destination.read_text(encoding="utf-8"))
                ):
                    problems.append(f"{name}:{number} links to {target}, which has no heading with that slug")
    return problems


THREAT_MODEL_DOC = OPERATIONS_CONTRACT
THREAT_MODEL_HEADING = "## Threat model"
THREAT_MODEL_SUITES = "skills/ejryan89-code-review-core/scripts"
# The suite that holds the threat model one row at a time: every row names at least one of its tests.
THREAT_MODEL_ADVERSARIAL_SUITE = "test_adversarial_inputs.py"
# Where a Python file a document names by its file name alone is; one named with a directory is from the repository
# root, and one no entry here places is any Python file the repository holds under that name.
BARE_SUITE_HOMES = {THREAT_MODEL_DOC: THREAT_MODEL_SUITES}
# Markdown whose cited tests are not checked: fixtures are inputs, not documents, and a release's upgrade notes name
# tests as they were at that release.
CITATIONS_UNREAD = ("tests/fixtures/", ".claude/worktrees/")
CITATIONS_UNREAD_FILES = frozenset({"docs/upgrade-notes.md"})
# A cited Python file, and a cited test ID: a Python file, a test function or test class in it, and after a class one
# of its tests. Each is the whole of a code span.
CITED_FILE = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.py")
CITED_ID = re.compile(rf"({CITED_FILE.pattern})::(\w+)(?:::(\w+))?")
# A cited test or test class by its name alone, the whole of a code span.
CITED_NAME = re.compile(r"test_\w+|[A-Z]\w*Tests?|Test[A-Z]\w*")
# Where a sentence ends in the text between code spans: a table cell's edge, or a full stop, question mark, or
# exclamation mark before a space or the line's end.
SENTENCE_END = re.compile(r"\||[.!?](?=\s|$)")


def _definitions(body: list[ast.stmt]) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """The functions and classes a module or class body defines, those under an `if`, `try`, or `with` included, and
    never one nested in a function."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node
        else:
            for field in ("body", "orelse", "finalbody"):
                yield from _definitions(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _definitions(handler.body)


def _test_names(body: list[ast.stmt]) -> set[str]:
    return {
        node.name
        for node in _definitions(body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
    }


@dataclass
class SuiteTests:
    """The tests one Python file defines: each test function, a module-level function or a class's method named
    `test...`, and each test class, a module-level class whose tests, its own or those of a base class the file
    defines, are listed by its name. A helper is neither."""

    functions: set[str] = field(default_factory=set)
    classes: dict[str, set[str]] = field(default_factory=dict)

    def defines(self, name: str) -> bool:
        return name in self.functions or name in self.classes


def _suite_tests(tree: ast.Module) -> SuiteTests:
    classes = {node.name: node for node in _definitions(tree.body) if isinstance(node, ast.ClassDef)}

    def tests_of(name: str, seen: frozenset[str]) -> set[str]:
        node = classes[name]
        inherited = [base.id for base in node.bases if isinstance(base, ast.Name) and base.id in classes]
        return _test_names(node.body).union(*(tests_of(base, seen | {name}) for base in inherited if base not in seen))

    suite = SuiteTests(functions=_test_names(tree.body))
    for name in classes:
        methods = tests_of(name, frozenset())
        if methods:
            suite.functions |= methods
            suite.classes[name] = methods
    return suite


class _TestIndex:
    """The tests of the repository's Python files, each file read once, when a citation first needs it."""

    def __init__(self, root: Path, problems: list[str]) -> None:
        self.root = root
        self.problems = problems
        self.python = sorted(
            (
                path
                for path in repository_files(root)
                if path.suffix == ".py" and not path.relative_to(root).as_posix().startswith(CITATIONS_UNREAD)
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        self.read: dict[Path, SuiteTests] = {}

    def suite(self, path: Path) -> SuiteTests:
        if path not in self.read:
            self.read[path] = SuiteTests()
            for _, _, tree in python_trees([path], self.root, self.problems):
                self.read[path] = _suite_tests(tree)
        return self.read[path]

    def locations(self, document: str, suite: str) -> tuple[str, list[Path]]:
        """Where a document's cited suite is, as the citation's message names it, and the files there."""
        if "/" in suite or document in BARE_SUITE_HOMES:
            location = suite if "/" in suite else f"{BARE_SUITE_HOMES[document]}/{suite}"
            return location, [self.root / location] if (self.root / location).is_file() else []
        return f"any file named {suite}", [path for path in self.python if path.name == suite]

    def defined_anywhere(self, name: str) -> bool:
        return any(self.suite(path).defines(name) for path in self.python)


def _sentences(line: str) -> Iterator[list[str]]:
    """The code spans of a line's sentences, one list for each sentence that holds one."""
    spans: list[str] = []
    end = 0
    for match in COMMONMARK_CODE_SPAN.finditer(line):
        if spans and SENTENCE_END.search(line, end, match.start()):
            yield spans
            spans = []
        spans.append(match.group(2).strip())
        end = match.end()
    if spans:
        yield spans


def _id_problem(index: _TestIndex, document: str, citation: re.Match[str]) -> str | None:
    suite, name, test = citation.groups()
    location, paths = index.locations(document, suite)
    if not paths:
        return f"cites {citation.group(0)}, but {location} does not exist"
    for path in paths:
        tests = index.suite(path)
        if (test is None and tests.defines(name)) or test in tests.classes.get(name, ()):
            return None
    kind = f"a test of its class {name}" if test is not None else "a test or a test class"
    return f"cites {citation.group(0)}, which {location} does not define as {kind}"


def _name_problem(index: _TestIndex, document: str, name: str, files: list[str]) -> str | None:
    if not files:
        if index.defined_anywhere(name):
            return None
        return f"cites {name}, which no Python file defines as a test or a test class"
    located = [index.locations(document, file) for file in files]
    paths = [path for _, found in located for path in found]
    if not paths:
        return f"cites {name} in {listed([location for location, _ in located])}, which does not exist"
    if any(index.suite(path).defines(name) for path in paths):
        return None
    return f"cites {name} in {listed(files)}, where no test or test class has that name"


def cited_test_problems(root: Path) -> list[str]:
    """Report each test a Markdown file cites that is not where it says: a test ID, `<file>::<test>`,
    `<file>::<class>`, or `<file>::<class>::<test>`, and a test or test class named alone, which must be in a Python
    file its sentence names, such as a suite or a policy module, and else in any Python file. A test is a function or
    method named `test...` and a test class a class with one, never a helper. Fixtures, upgrade notes, files Git
    ignores, and the session worktrees are not read."""
    problems: list[str] = []
    index = _TestIndex(root, problems)
    for path in sorted(repository_files(root), key=lambda path: path.relative_to(root).as_posix()):
        document = path.relative_to(root).as_posix()
        if (
            path.suffix.casefold() != ".md"
            or document.startswith(CITATIONS_UNREAD)
            or document in CITATIONS_UNREAD_FILES
        ):
            continue
        for number, line in enumerate(_outside_fences(path.read_text(encoding="utf-8")), start=1):
            for spans in _sentences(line):
                files = [span for span in spans if CITED_FILE.fullmatch(span)]
                for span in spans:
                    if citation := CITED_ID.fullmatch(span):
                        problem = _id_problem(index, document, citation)
                    elif CITED_NAME.fullmatch(span):
                        problem = _name_problem(index, document, span, files)
                    else:
                        continue
                    if problem:
                        problems.append(f"{document}:{number} {problem}")
    return problems


def _guard_problem(root: Path, index: _TestIndex, document: str, span: str) -> str | None:
    """Why a guard a lesson names does not resolve, or None when it does: a test ID by its path from the repository
    root, a heading of a skill's SKILL.md by its fragment, or a workflow file."""
    skill_step = re.compile(r"((?:\.claude/)?skills/[\w.-]+/SKILL\.md)#([\w-]+)")
    workflow = re.compile(r"\.github/workflows/[\w.-]+\.ya?ml")
    if (citation := CITED_ID.fullmatch(span)) and "/" in citation.group(1):
        return _id_problem(index, document, citation)
    if step := skill_step.fullmatch(span):
        path = root / step.group(1)
        if not path.is_file():
            return f"cites {span}, but {step.group(1)} does not exist"
        if step.group(2) not in heading_slugs(path.read_text(encoding="utf-8")):
            return f"cites {span}, which names no heading of {step.group(1)}"
        return None
    if workflow.fullmatch(span):
        return None if (root / span).is_file() else f"cites {span}, but {span} does not exist"
    return f"names {span}, which is not a test ID, a skill's step, or a workflow by its path from the repository root"


def _bound_names(body: list[ast.stmt]) -> dict[str, ast.stmt]:
    """The names a module or class body binds, to the statement that binds each: its functions and classes, those
    under an `if`, `try`, or `with` included, and each name a plain or annotated assignment binds in the body itself."""
    names: dict[str, ast.stmt] = {node.name: node for node in _definitions(body)}
    for node in body:
        if isinstance(node, ast.Assign):
            names |= {target.id: node for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] = node
    return names


def _use_problem(root: Path, span: str) -> str | None:
    """Why a mechanism a pattern uses does not resolve, or None when it does: a module and a name it binds at module
    level, `<module>:<name>`, or a name one of its classes binds, `<module>:<class>.<name>`, as `pkgutil.resolve_name`
    reads an object, where the module is importable from the repository root or from skill-core's scripts, the two
    directories every part of the repository puts on `sys.path`."""
    mechanism = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?")
    if not (use := mechanism.fullmatch(span)):
        return f"names {span}, which is not a module and a name, <module>:<name> or <module>:<class>.<name>"
    module, name, member = use.groups()
    parts = module.split(".")
    candidates = [
        base / Path(*parts[:-1]) / last
        for base in (root, root / SKILL_CORE_SCRIPTS)
        for last in (f"{parts[-1]}.py", f"{parts[-1]}/__init__.py")
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return f"uses {span}, but no module {module} is importable from the repository root or {SKILL_CORE_SCRIPTS}"
    module = path.relative_to(root).as_posix()
    unreadable: list[str] = []
    names = {
        bound: node
        for _, _, tree in python_trees([path], root, unreadable)
        for bound, node in _bound_names(tree.body).items()
    }
    if unreadable:
        return f"uses {span}, but {unreadable[0]}"
    if name not in names:
        return f"uses {span}, which {module} does not define at module level"
    if member is None:
        return None
    if not isinstance(owner := names[name], ast.ClassDef):
        return f"uses {span}, but {name} in {module} is not a class"
    if member not in _bound_names(owner.body):
        return f"uses {span}, which the class {name} of {module} does not define"
    return None


def _pattern_fields(entry: list[tuple[int, str]]) -> tuple[dict[str, list[tuple[int, str]]], list[tuple[int, str]]]:
    """A pattern's fields, each the lines of its list item, and the problems, by line, of a field the catalogue does
    not define or one given twice."""
    known = {"Rule", "Why", "Use", "Guard", "No guard", "Search for", "Examples"}
    field_line = re.compile(r"^- \*\*([^*:]+):\*\*(.*)$")
    fields: dict[str, list[tuple[int, str]]] = {}
    problems: list[tuple[int, str]] = []
    current: list[tuple[int, str]] | None = None
    for number, line in entry[1:]:
        if match := field_line.match(line):
            name = match.group(1)
            if name not in known:
                problems.append((number, f"names the field {name!r}, which a pattern does not have"))
            elif name in fields:
                problems.append((number, f"names the field {name!r} more than once"))
            current = fields.setdefault(name, [])
            current.append((number, match.group(2).strip()))
        elif current is not None and line[:1].isspace() and line.strip():
            # A field's continuation line, indented under its list item, is read with the line it continues.
            current.append((number, line.strip()))
        else:
            current = None
    return fields, problems


def _spans(lines: list[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    """Each line of a field with the text of the code spans it holds."""
    return [
        (number, [match.group(2).strip() for match in COMMONMARK_CODE_SPAN.finditer(text)]) for number, text in lines
    ]


def _entry_problems(root: Path, index: _TestIndex, entry: list[tuple[int, str]]) -> list[str]:
    """The problems of one pattern, its heading line first and then its lines up to the next heading."""
    document = "docs/lessons.md"
    heading_number, heading = entry[0]
    name = heading[4:].strip()

    def at(number: int) -> str:
        return f"{document}:{number} the pattern {name!r}"

    fields, field_problems = _pattern_fields(entry)
    problems = [f"{at(number)} {problem}" for number, problem in field_problems]
    problems += [
        f"{at(heading_number)} states no {field}"
        for field in ("Rule", "Why")
        if not any(text for _, text in fields.get(field, []))
    ]
    if not any(spans for _, spans in _spans(fields.get("Search for", []))):
        problems.append(f"{at(heading_number)} names no form a violation takes under Search for, each in a code span")
    guards, uses = _spans(fields.get("Guard", [])), _spans(fields.get("Use", []))
    reasons = [text for _, text in fields.get("No guard", []) if text]
    if guards and reasons:
        return [*problems, f"{at(heading_number)} names both a guard and the reason no guard is possible"]
    if not guards and not uses and not reasons:
        return [
            *problems,
            f"{at(heading_number)} names neither its mechanism, a guard, nor the reason no guard is possible",
        ]
    for kind, lines, resolve in (
        ("guard", guards, lambda span: _guard_problem(root, index, document, span)),
        ("mechanism", uses, lambda span: _use_problem(root, span)),
    ):
        if lines and not any(spans for _, spans in lines):
            problems.append(f"{at(lines[0][0])} names no {kind} in a code span")
        for number, spans in lines:
            problems += [f"{at(number)} {problem}" for span in spans if (problem := resolve(span))]
    return problems


def lessons_ledger_problems(root: Path) -> list[str]:
    """Report each pattern of docs/lessons.md, a `###` heading under its `## Patterns` section, that states no Rule or
    Why, names no form a violation takes under Search for in a code span, names neither the mechanism it uses, a guard
    that refuses a violation, nor the reason no guard is possible, or names both a guard and that reason, or names a
    field a pattern does not have or one field twice; and each guard and mechanism it names that does not resolve. A
    guard is a test ID, `<path>::<test>`, `<path>::<class>`, or `<path>::<class>::<test>`, that names a test or a test
    class of that file, a skill's step, `<path>/SKILL.md#<fragment>`, that names a heading of that SKILL.md, or a
    workflow, `.github/workflows/<file>`, that exists. A mechanism is a module importable from the repository root or
    skill-core's scripts and a name it binds, `<module>:<name>` or `<module>:<class>.<name>`."""
    document = "docs/lessons.md"
    path = root / document
    if not path.is_file():
        return [f"{document} does not exist"]
    lines = _outside_fences(path.read_text(encoding="utf-8"))
    start = next((number for number, line in enumerate(lines) if line.rstrip() == "## Patterns"), None)
    if start is None:
        return [f"{document} has no '## Patterns' section"]
    entries: list[list[tuple[int, str]]] = []
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if line.startswith("### "):
            entries.append([])
        if entries:
            entries[-1].append((number, line))
    if not entries:
        return [f"{document} has no pattern under '## Patterns'"]
    problems: list[str] = []
    index = _TestIndex(root, problems)
    for entry in entries:
        problems += _entry_problems(root, index, entry)
    return problems


def threat_model_test_problems(root: Path) -> list[str]:
    """Report each row of the code-review threat model that names no test in test_adversarial_inputs.py by its test
    ID; cited_test_problems holds each test the rows cite to its suite."""
    section = markdown_section((root / THREAT_MODEL_DOC).read_text(encoding="utf-8"), THREAT_MODEL_HEADING)
    rows = [line for line in (section or "").split("\n") if line.startswith("|")][2:]
    if not rows:
        return [f"{THREAT_MODEL_DOC} has no table under {THREAT_MODEL_HEADING!r}"]
    problems: list[str] = []
    for row in rows:
        cited = [CITED_ID.fullmatch(span.strip()) for _, span in COMMONMARK_CODE_SPAN.findall(row)]
        if all(citation is None or citation.group(1) != THREAT_MODEL_ADVERSARIAL_SUITE for citation in cited):
            problems.append(
                f"{THREAT_MODEL_DOC}: the threat-model row {row.split('|')[1].strip()!r} names no test in "
                f"{THREAT_MODEL_ADVERSARIAL_SUITE}"
            )
    return problems


TRUST_MODEL_DOC = "docs/design.md"
TRUST_MODEL_HEADING = "## Trust model"
SECURITY_DOC = "SECURITY.md"
SECURITY_AREAS_HEADING = "## Security-sensitive areas"
# A list item of the security-sensitive areas that opens with a bold name is a boundary's area, the name being the
# boundary's as the trust model's first column gives it; an item with no bold opening names no boundary.
SECURITY_AREA = re.compile(r"^- \*\*(.+?)\*\*")


def trust_model_area_problems(root: Path) -> list[str]:
    """Report each boundary of the trust model that no security-sensitive area of SECURITY.md names, and each area
    whose bold name is no boundary of the trust model, so a reporter reading SECURITY.md alone is told of every
    boundary the design places."""
    section = markdown_section((root / TRUST_MODEL_DOC).read_text(encoding="utf-8"), TRUST_MODEL_HEADING)
    rows = [line for line in (section or "").split("\n") if line.startswith("|")][2:]
    if not rows:
        return [f"{TRUST_MODEL_DOC} has no table under {TRUST_MODEL_HEADING!r}"]
    boundaries = [row.split("|")[1].strip() for row in rows]
    areas_section = markdown_section((root / SECURITY_DOC).read_text(encoding="utf-8"), SECURITY_AREAS_HEADING)
    if areas_section is None:
        return [f"{SECURITY_DOC} has no {SECURITY_AREAS_HEADING!r} section"]
    areas = [match.group(1) for line in areas_section.split("\n") if (match := SECURITY_AREA.match(line))]
    problems = [
        f"{SECURITY_DOC}: no security-sensitive area names the trust-model boundary {boundary!r} of {TRUST_MODEL_DOC}"
        for boundary in boundaries
        if boundary not in areas
    ]
    problems += [
        f"{SECURITY_DOC}: the security-sensitive area {area!r} names no trust-model boundary of {TRUST_MODEL_DOC}"
        for area in areas
        if area not in boundaries
    ]
    return problems


class MarkdownLinksPolicies(unittest.TestCase):
    def test_repository_links_resolve(self) -> None:
        self.assertEqual([], markdown_link_problems(REPOSITORY_ROOT))

    def test_every_threat_model_row_names_an_adversarial_test(self) -> None:
        self.assertEqual([], threat_model_test_problems(REPOSITORY_ROOT))

    def test_every_test_a_document_cites_exists(self) -> None:
        self.assertEqual([], cited_test_problems(REPOSITORY_ROOT))

    def test_every_pattern_names_its_search_and_a_mechanism_guard_or_reason_that_resolves(self) -> None:
        self.assertEqual([], lessons_ledger_problems(REPOSITORY_ROOT))

    def test_every_trust_model_boundary_has_a_security_sensitive_area(self) -> None:
        self.assertEqual([], trust_model_area_problems(REPOSITORY_ROOT))

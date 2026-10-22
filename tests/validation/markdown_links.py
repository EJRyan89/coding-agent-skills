"""Documents that point elsewhere keep pointing at something: relative Markdown links and their fragments, and the
tests a document cites.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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


class MarkdownLinksPolicies(unittest.TestCase):
    def test_repository_links_resolve(self) -> None:
        self.assertEqual([], markdown_link_problems(REPOSITORY_ROOT))

    def test_every_threat_model_row_names_an_adversarial_test(self) -> None:
        self.assertEqual([], threat_model_test_problems(REPOSITORY_ROOT))

    def test_every_test_a_document_cites_exists(self) -> None:
        self.assertEqual([], cited_test_problems(REPOSITORY_ROOT))

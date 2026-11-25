"""Documents that point elsewhere keep pointing at something: relative Markdown links and their fragments, the
tests a document cites, the mechanism and guard each pattern of docs/lessons.md names, the security-sensitive area
.github/SECURITY.md gives each boundary of docs/design.md's trust model, and the new path of each file moved off the
repository root.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import file_presence
import lessons_catalogue
from evaluation_results import TableError, measured_scenarios
from link_destinations import link_destinations
from markdown_sections import ATX_HEADING, code_spans, outside_fences
from text_lines import text_lines
from validation_support import (
    OPERATIONS_CONTRACT,
    REPOSITORY_ROOT,
    listed,
    markdown_section,
    python_trees,
    repository_files,
)

# GitHub's heading slug keeps letters, digits, underscores, hyphens, and spaces, then turns each space into a hyphen.
SLUG_REMOVED = re.compile(r"[^\w\- ]")
# The fields only a pattern under the catalogue's Prompt patterns has.
PROMPT_FIELDS = ("Prompt", "Caught by")
# A prompt a lesson names: a file by its path from the repository root, and a heading of it by its fragment.
PROMPT_PLACE = re.compile(r"([\w.-]+(?:/[\w.-]+)*)(?:#([\w-]+))?")
# An evaluation scenario a lesson names as its guard, by its folder: the skill, and the scenario.
EVALUATION_SCENARIO = re.compile(r"tests/fixtures/skill-evals/([\w.-]+)/([\w.-]+)")
# The document whose results table records each skill's evaluation runs.
EVALUATIONS_DOC = "docs/skill-evaluations.md"


PRESENCE_ALLOWED = {
    "markdown_link_problems": (
        "names each case: is_there tells something of another kind from nothing there, and a stat that fails raises"
    ),
}


def heading_slugs(text: str) -> set[str]:
    """The fragment GitHub gives each heading, numbering a repeated one -1, -2, and so on."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for line in outside_fences(text_lines(text)):
        heading = ATX_HEADING.match(line)
        if heading:
            slug = SLUG_REMOVED.sub("", heading.group("text").strip().lower()).replace(" ", "-")
            slugs.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
            seen[slug] = seen.get(slug, 0) + 1
    return slugs


def markdown_link_problems(root: Path) -> list[str]:
    """Report each relative link in the repository's Markdown to a missing file, or to a heading its target lacks.

    Links are read through ejryan89-skill-core's `link_destinations`, as every reader of a link destination reads them,
    so code blocks and spans are not links, and a destination is percent-decoded; URLs with a scheme are not checked.
    Files Git ignores, and the session worktrees under .claude/worktrees/, are not read.

    Limit: a destination is resolved on the file system, not within the repository as GitHub resolves it. One that
    opens with `/` is read from the file system's root, so a link to `/docs/x.md` is reported missing however the
    repository holds that file, and a `..` that climbs past the repository root leaves it, so a link to a file beside
    the checkout passes. No document in the repository writes either.
    """
    problems: list[str] = []
    for path in sorted(repository_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.suffix.casefold() != ".md" or name.startswith(".claude/worktrees/"):
            continue
        for link in link_destinations(text_lines(path.read_text(encoding="utf-8"))):
            if link.is_url:
                continue
            number, target, fragment = link.line, link.url, link.fragment
            destination = path.parent / link.path if link.path else path
            # A destination whose stat fails raises, never reads as one that does not exist or holds no heading.
            is_file = file_presence.is_present(destination, other_is_absent=True)
            if not is_file and not file_presence.is_directory(destination, other_is_absent=True):
                # Something that is neither a file nor a folder is named so, never as nothing there; a link to
                # nothing is nothing there, since nothing can be read through it.
                state = "is neither a file nor a folder" if file_presence.is_there(destination) else "does not exist"
                problems.append(f"{name}:{number} links to {target}, which {state}")
            elif (
                fragment
                and is_file
                and destination.suffix.casefold() == ".md"
                and fragment not in heading_slugs(destination.read_text(encoding="utf-8"))
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
            for block in (getattr(node, "body", []), getattr(node, "orelse", []), getattr(node, "finalbody", [])):
                yield from _definitions(block)
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

    def tests_of(name: str, seen: set[str]) -> set[str]:
        # Each class is read once per question: its tests depend only on the class, and the answer is a union, so a
        # base two bases share, or one a cycle of bases reaches, adds nothing.
        seen.add(name)
        node = classes[name]
        inherited = [base.id for base in node.bases if isinstance(base, ast.Name) and base.id in classes]
        return _test_names(node.body).union(*(tests_of(base, seen) for base in inherited if base not in seen))

    suite = SuiteTests(functions=_test_names(tree.body))
    for name in classes:
        methods = tests_of(name, set())
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

    def locations(self, document: str, suite: str) -> tuple[str, list[Path], bool]:
        """Where a document's cited suite is, as the citation's message names it, the files there, and whether
        something other than a file stands at a named location. A stat that fails raises."""
        if "/" in suite or document in BARE_SUITE_HOMES:
            location = suite if "/" in suite else f"{BARE_SUITE_HOMES[document]}/{suite}"
            try:
                present = file_presence.is_present(self.root / location)
            except file_presence.NotAFile:
                return location, [], True
            return location, [self.root / location] if present else [], False
        return f"any file named {suite}", [path for path in self.python if path.name == suite], False

    def defined_anywhere(self, name: str) -> bool:
        return any(self.suite(path).defines(name) for path in self.python)


def _sentences(line: str) -> Iterator[list[str]]:
    """The code spans of a line's sentences, one list for each sentence that holds one."""
    spans: list[str] = []
    end = 0
    for span in code_spans(line):
        if spans and SENTENCE_END.search(line, end, span.start):
            yield spans
            spans = []
        spans.append(span.text.strip())
        end = span.end
    if spans:
        yield spans


def _id_problem(index: _TestIndex, document: str, citation: re.Match[str]) -> str | None:
    suite, name, test = citation.groups()
    location, paths, wrong_kind = index.locations(document, suite)
    if wrong_kind:
        return f"cites {citation.group(0)}, but {location} is not a file"
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
    paths = [path for _, found, _ in located for path in found]
    if wrong := [location for location, _, wrong_kind in located if wrong_kind]:
        return f"cites {name} in {listed(wrong)}, which is not a file"
    if not paths:
        return f"cites {name} in {listed([location for location, _, _ in located])}, which does not exist"
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
        for number, line in enumerate(outside_fences(text_lines(path.read_text(encoding="utf-8"))), start=1):
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


def _scenario_problem(root: Path, span: str, skill: str, scenario: str) -> str | None:
    """Why an evaluation scenario a lesson names as its guard does not resolve, or None when it does: its folder is
    there, and a row of the results table in docs/skill-evaluations.md records it as measured, as
    ejryan89-skill-core's evaluation_results reads the table through `measured_scenarios`, never as not run."""
    # As for a skill step: a stat that fails raises, and a scenario or a results file of the wrong kind is named so.
    try:
        present = file_presence.is_directory(root / span)
    except file_presence.NotADirectory:
        return f"cites {span}, but {span} is not a folder"
    if not present:
        return f"cites {span}, but {span} does not exist"
    results = root / EVALUATIONS_DOC
    try:
        if not file_presence.is_present(results):
            return f"cites {span}, but {EVALUATIONS_DOC}, which records each scenario's runs, does not exist"
    except file_presence.NotAFile:
        return f"cites {span}, but {EVALUATIONS_DOC}, which records each scenario's runs, is not a file"
    try:
        measured = measured_scenarios(results.read_text(encoding="utf-8"), skill)
    except (OSError, UnicodeDecodeError, TableError) as error:
        return f"cites {span}, but {EVALUATIONS_DOC} cannot be read: {error}"
    if scenario not in measured:
        return f"cites {span}, which no row of {EVALUATIONS_DOC} records as run for {skill}"
    return None


def _guard_problem(root: Path, index: _TestIndex, document: str, span: str) -> str | None:
    """Why a guard a lesson names does not resolve, or None when it does: a test ID by its path from the repository
    root, a heading of a skill's SKILL.md by its fragment, a workflow file, or an evaluation scenario, which
    evaluate-skill runs on models and validation never does, by its folder."""
    skill_step = re.compile(r"((?:\.claude/)?skills/[\w.-]+/SKILL\.md)#([\w-]+)")
    workflow = re.compile(r"\.github/workflows/[\w.-]+\.ya?ml")
    if scenario := EVALUATION_SCENARIO.fullmatch(span):
        return _scenario_problem(root, span, scenario.group(1), scenario.group(2))
    if (citation := CITED_ID.fullmatch(span)) and "/" in citation.group(1):
        return _id_problem(index, document, citation)
    if step := skill_step.fullmatch(span):
        path = root / step.group(1)
        # A file whose stat fails raises, and one of the wrong kind is named so, never reported as one that does not
        # exist.
        try:
            present = file_presence.is_present(path)
        except file_presence.NotAFile:
            return f"cites {span}, but {step.group(1)} is not a file"
        if not present:
            return f"cites {span}, but {step.group(1)} does not exist"
        if step.group(2) not in heading_slugs(path.read_text(encoding="utf-8")):
            return f"cites {span}, which names no heading of {step.group(1)}"
        return None
    if workflow.fullmatch(span):
        # As for a skill step: a stat that fails raises, and a workflow of the wrong kind is named so.
        try:
            present = file_presence.is_present(root / span)
        except file_presence.NotAFile:
            return f"cites {span}, but {span} is not a file"
        return None if present else f"cites {span}, but {span} does not exist"
    return (
        f"names {span}, which is not a test ID, a skill's step, a workflow, or an evaluation scenario by its path from "
        "the repository root"
    )


def _prompt_problem(root: Path, span: str) -> str | None:
    """Why a prompt a lesson names does not resolve, or None when it does: a file by its path from the repository
    root, inside it, and, for a Markdown file, a heading of it by its fragment."""
    if not (prompt := PROMPT_PLACE.fullmatch(span)) or ".." in prompt.group(1).split("/"):
        return f"names {span}, which is not a file by its path from the repository root, with a heading's fragment"
    name, fragment = prompt.groups()
    try:
        present = file_presence.is_present(root / name)
    except file_presence.NotAFile:
        return f"names the prompt {span}, but {name} is not a file"
    if not present:
        return f"names the prompt {span}, but {name} does not exist"
    if fragment is None:
        return None
    if not name.endswith(".md"):
        return f"names the prompt {span}, but {name} is not Markdown, whose headings a fragment names"
    try:
        text = (root / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"names the prompt {span}, but {name} cannot be read: {error}"
    if fragment not in heading_slugs(text):
        return f"names the prompt {span}, which names no heading of {name}"
    return None


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
    reads an object, where the module is importable from exactly one of the directories a part of the repository puts
    on `sys.path`: the repository root, or a skill's or repository skill's `scripts`."""
    if not (use := lessons_catalogue.MECHANISM.fullmatch(span)):
        return f"names {span}, which is not a module and a name, <module>:<name> or <module>:<class>.<name>"
    module, name, member = use.groups()
    parts = module.split(".")
    bases = lessons_catalogue.mechanism_bases(root)
    found = [
        candidate
        for base in bases
        for candidate in (base / Path(*parts[:-1]) / f"{parts[-1]}.py", base / Path(*parts) / "__init__.py")
        if file_presence.is_present(candidate, other_is_absent=True)
    ]
    if not found:
        return f"uses {span}, but no module {module} is importable from the repository root or a skill's scripts"
    if len(found) > 1:
        places = listed([path.relative_to(root).as_posix() for path in found])
        return f"uses {span}, but the module {module} is ambiguous: {places}"
    path = found[0]
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


def _entry_problems(root: Path, index: _TestIndex, pattern: lessons_catalogue.Pattern) -> list[str]:
    """The problems of one pattern the catalogue reader read."""
    document = lessons_catalogue.CATALOGUE
    heading_number, name, fields = pattern.line, pattern.name, pattern.fields

    def at(number: int) -> str:
        return f"{document}:{number} the pattern {name!r}"

    problems = [f"{at(number)} {problem}" for number, problem in pattern.problems]
    prompted = pattern.section == lessons_catalogue.PROMPT_PATTERNS
    problems += [
        f"{at(fields[field][0][0])} names the field {field!r}, which only a pattern under "
        f"'## {lessons_catalogue.PROMPT_PATTERNS}' has"
        for field in PROMPT_FIELDS
        if not prompted and field in fields
    ]
    problems += [
        f"{at(heading_number)} states no {field}"
        for field in ("Rule", "Why", *(("Prompt",) if prompted else ()))
        if not any(text for _, text in fields.get(field, []))
    ]
    prompts = pattern.spans("Prompt") if prompted else []
    if any(text for _, text in fields.get("Prompt", [])) and prompted and not any(spans for _, spans in prompts):
        problems.append(f"{at(prompts[0][0])} names no prompt in a code span")
    for number, spans in prompts:
        problems += [f"{at(number)} {problem}" for span in spans if (problem := _prompt_problem(root, span))]
    if not any(spans for _, spans in pattern.spans("Search for")):
        problems.append(f"{at(heading_number)} names no form a violation takes under Search for, each in a code span")
    guards, uses = pattern.spans("Guard"), pattern.spans("Use")
    reasons = [text for _, text in fields.get("No guard", []) if text]
    if guards and reasons:
        return [*problems, f"{at(heading_number)} names both a guard and the reason no guard is possible"]
    if not guards and not reasons:
        return [
            *problems,
            f"{at(heading_number)} names neither a Guard nor, under No guard, the reason no guard is possible",
        ]
    resolvers: tuple[tuple[str, list[tuple[int, list[str]]], Callable[[str], str | None]], ...] = (
        ("guard", guards, lambda span: _guard_problem(root, index, document, span)),
        ("mechanism", uses, lambda span: _use_problem(root, span)),
    )
    for kind, lines, resolve in resolvers:
        if lines and not any(spans for _, spans in lines):
            problems.append(f"{at(lines[0][0])} names no {kind} in a code span")
        for number, spans in lines:
            problems += [f"{at(number)} {problem}" for span in spans if (problem := resolve(span))]
    return problems


def lessons_ledger_problems(root: Path) -> list[str]:
    """Report each pattern of docs/lessons.md, under its Patterns or its Prompt patterns, read through
    ejryan89-skill-core's lessons_catalogue, the reader the `implement-change` repository skill's pattern lister shares,
    that states no Rule or Why, or, for a prompt pattern, no Prompt, names no form a violation takes under Search for in
    a code span, names neither a guard that refuses a violation nor, under No guard, the reason no guard is possible,
    whatever mechanism it uses, or names both a guard and that reason, names a field a pattern does not have or one
    field twice, or, outside Prompt patterns, a Prompt or a Caught by; each prompt, guard, and mechanism it names that
    does not resolve; and a catalogue that is missing, cannot be read, or holds no pattern. A prompt is a file by its
    path from the repository root, `<path>`, or, for Markdown, one of its headings, `<path>#<fragment>`. A guard is a
    test ID, `<path>::<test>`, `<path>::<class>`, or `<path>::<class>::<test>`, that names a test or a test class of
    that file, a skill's step, `<path>/SKILL.md#<fragment>`, that names a heading of that SKILL.md, a workflow,
    `.github/workflows/<file>`, that exists, or an evaluation scenario, `tests/fixtures/skill-evals/<skill>/<scenario>`,
    a folder that a row of docs/skill-evaluations.md records, which evaluate-skill runs on models, never validation. A
    mechanism is a module importable from exactly one of the repository root and the skills' scripts, and a name it
    binds, `<module>:<name>` or `<module>:<class>.<name>`."""
    try:
        patterns = lessons_catalogue.read_patterns(root)
    except lessons_catalogue.CatalogueError as error:
        return [str(error)]
    problems: list[str] = []
    index = _TestIndex(root, problems)
    for pattern in patterns:
        problems += _entry_problems(root, index, pattern)
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
        cited = [CITED_ID.fullmatch(span.text.strip()) for span in code_spans(row)]
        if all(citation is None or citation.group(1) != THREAT_MODEL_ADVERSARIAL_SUITE for citation in cited):
            problems.append(
                f"{THREAT_MODEL_DOC}: the threat-model row {row.split('|')[1].strip()!r} names no test in "
                f"{THREAT_MODEL_ADVERSARIAL_SUITE}"
            )
    return problems


TRUST_MODEL_DOC = "docs/design.md"
TRUST_MODEL_HEADING = "## Trust model"
SECURITY_DOC = ".github/SECURITY.md"
SECURITY_AREAS_HEADING = "## Security-sensitive areas"
# A list item of the security-sensitive areas that opens with a bold name is a boundary's area, the name being the
# boundary's as the trust model's first column gives it; an item with no bold opening names no boundary.
SECURITY_AREA = re.compile(r"^- \*\*(.+?)\*\*")


def trust_model_area_problems(root: Path) -> list[str]:
    """Report each boundary of the trust model that no security-sensitive area of .github/SECURITY.md names, and each
    area whose bold name is no boundary of the trust model, so a reporter reading .github/SECURITY.md alone is told of
    every boundary the design places."""
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


# The files moved off the repository root, each by the name it had there and the folder that holds it now: GitHub reads
# the community files from .github/ as it reads them from the root, and the dependency pins sit in one folder, from
# which pip reads an include and pip-tools and Dependabot compile a lock.
MOVED_OFF_ROOT = {
    "CONTRIBUTING.md": ".github",
    "CODE_OF_CONDUCT.md": ".github",
    "SECURITY.md": ".github",
    "requirements-dev.in": "requirements",
    "requirements-dev.txt": "requirements",
    "requirements-mutation.in": "requirements",
    "requirements-mutation.txt": "requirements",
}
# A moved file named from the root, bare or climbing to it with `../`: no folder, other name, or extension runs into it.
ROOT_NAMED = re.compile(r"(?<![\w./-])(?:\.\./)*(" + "|".join(map(re.escape, MOVED_OFF_ROOT)) + r")(?![\w-]|\.\w)")
# What moved_path_problems does not read: a shipped skill's or agent's Markdown, which names the files of the
# repository it examines, such as that repository's own CONTRIBUTING.md; fixtures, which ship nothing; the session
# worktrees; and the release notes, which record the paths each release had.
MOVED_PATHS_UNREAD = ("skills/", "agents/", "tests/fixtures/", ".claude/worktrees/")
MOVED_PATHS_UNREAD_FILES = frozenset({"docs/upgrade-notes.md"})
# A line that compiles a lock runs in the pins' folder, as each lock's header records the command, so it names the
# pins from there.
LOCK_COMPILE = "piptools compile"


def moved_path_problems(root: Path) -> list[str]:
    """Report each place a document, which is Markdown or any file under .github/, names a file of MOVED_OFF_ROOT
    from the repository root, where it no longer is, rather than by its path in its new folder.

    A Markdown link's destination is left to markdown_link_problems, which reports one that names the old path
    missing, and a line that compiles a lock names the pins from their folder.

    Limits: a path written from the root with `./`, as `./SECURITY.md`, is not read as the root path, and a URL to
    the repository's file, as `https://github.com/<owner>/<repo>/blob/main/SECURITY.md`, is not read, and
    markdown_link_problems checks no URL. No document writes the first or links a moved file by URL.
    """
    problems: list[str] = []
    for path in sorted(repository_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if name in MOVED_PATHS_UNREAD_FILES or name.startswith(MOVED_PATHS_UNREAD):
            continue
        if path.suffix.casefold() != ".md" and not name.startswith(".github/"):
            continue
        for number, line in enumerate(text_lines(path.read_text(encoding="utf-8")), start=1):
            if LOCK_COMPILE in line:
                continue
            for found in ROOT_NAMED.finditer(line):
                if line[: found.start()].endswith("]("):
                    continue
                moved = found.group(1)
                problems.append(
                    f"{name}:{number} names {found.group(0)}, which moved to {MOVED_OFF_ROOT[moved]}/{moved}"
                )
    return problems


def moved_file_problems(root: Path) -> list[str]:
    """Report each file of MOVED_OFF_ROOT that is not a file in its new folder, or that is at the repository root
    again, so the table moved_path_problems reads stays the tree's."""
    problems: list[str] = []
    for moved, folder in MOVED_OFF_ROOT.items():
        reason = file_presence.refusal_reason(root / folder / moved)
        if reason is not None:
            problems.append(f"{folder}/{moved} {reason}, though MOVED_OFF_ROOT names it")
        if file_presence.is_there(root / moved):
            problems.append(f"{moved} is at the repository root, though MOVED_OFF_ROOT moved it to {folder}/")
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

    def test_no_document_names_a_moved_file_by_its_root_path(self) -> None:
        self.assertEqual([], moved_path_problems(REPOSITORY_ROOT))

    def test_each_moved_file_is_in_its_folder_and_not_at_the_root(self) -> None:
        self.assertEqual([], moved_file_problems(REPOSITORY_ROOT))

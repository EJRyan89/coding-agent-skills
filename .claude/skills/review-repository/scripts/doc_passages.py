"""The documentation-drift route's condition: find the passages of this repository's documents that name what a pull
request changes, so the reviewer compares each one with the change.

    python -B doc_passages.py --source-root <snapshot>

A review runs it as a specialists manifest's condition. Its environment names the pull request's unified diff in
CODE_REVIEW_DIFF_FILE and the file to write in CODE_REVIEW_CONDITION_OUTPUT. From the diff it takes the names to look
for: each changed script's file name (`skill_evals.py`) and, for Python, its module name (`skill_evals`), and each
top-level function or class a hunk adds, removes, or changes inside (`def`/`class` lines and the hunk header's
function context). It searches the documents under the snapshot (`docs/`, the top-level README.md, CONTRIBUTING.md, and
CLAUDE.md, and every skill's SKILL.md, but no release's notes or evaluation results) for those names, but only where
a document writes code: inside a code span or a fenced block. A file name counts as a path's last segment there; a
module, function, or class name counts as the whole span, a call (`name(`), or a dotted part (`module.name`). Prose
never counts, nor `main`, which every entry point defines, nor a name shorter than MIN_NAME_LENGTH, so a name that is
also a common word does not match every sentence that uses the word. A test file's names are left out.

It writes each matching passage (a paragraph, a list item, a table row, a heading, or a fenced block) as
`PASSAGE <document>:<line> names <name> (<kind> in <changed file>), hunk <header> at new lines <first>-<last>`, then
the passage, at most MAX_PASSAGES of them taken in turn from each name, the names fewest passages name first, with an
`OMITTED <n>` line for the rest.

Prints `PASSAGES <found> given=<n>`. Exit status 0 means it wrote passages, which opens the route; 1 that no document
names what changed, which closes it; 2 a usage error or a last line FAILED <reason>, which fails the review.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

from console import failed_line, run_main

EXIT_CONTRACT_EXEMPT = (
    "a code-review condition: exit 0 opens its route and 1 closes it, so a failure exits 2, which fails the review "
    "rather than silently closing the route"
)
DIFF_VARIABLE = "CODE_REVIEW_DIFF_FILE"
OUTPUT_VARIABLE = "CODE_REVIEW_CONDITION_OUTPUT"
# The documents searched, as globs under the snapshot; the route's `reads` in specialists.json declares the same.
DOCUMENTS = (
    "docs/**/*.md",
    "README.md",
    "CONTRIBUTING.md",
    "CLAUDE.md",
    "skills/**/SKILL.md",
    ".claude/skills/*/SKILL.md",
)
# Records of the past or of results, not descriptions of the code: a release's notes describe what it shipped then,
# and the evaluation results are written by a tool.
RECORDS = frozenset({"docs/upgrade-notes.md", "docs/skill-evaluations.md"})
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh")
# Names shorter than this are words a document uses in code for other things (`run`, `id`), so they never count.
MIN_NAME_LENGTH = 4
# Every entry point defines main, and documents write `main` for the branch.
IGNORED_NAMES = frozenset({"main"})
# A reviewer compares each passage with the change in turn; past this many the comparisons cost more than reading the
# area whole, which the release audit does, and the file stays under the CONDITION_FILE_BYTES a review gives it.
MAX_PASSAGES = 20
# A table row or a long paragraph is cut to this many characters around the name it matched.
MAX_PASSAGE_CHARACTERS = 1200

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
DEFINITION = re.compile(r"^(async\s+def|def|class)\s+([A-Za-z_]\w*)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
FENCE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass(frozen=True)
class Hunk:
    path: str
    header: str
    first: int
    last: int


@dataclass(frozen=True)
class Name:
    """A name to look for, what it names, and the hunk it relates to."""

    text: str
    kind: str  # "script", "module", "function", or "class"
    hunk: Hunk

    def pattern(self) -> re.Pattern[str]:
        word = re.escape(self.text)
        if self.kind == "script":
            return re.compile(rf"(?<![\w.-]){word}(?![\w-])")
        return re.compile(rf"^{word}$|(?<![\w-]){word}(?=\()|(?<=\.){word}(?![\w-])|(?<![\w.-]){word}\.(?=\w)")


@dataclass(frozen=True)
class Unit:
    """A block of a document a passage is: its first line and its lines, and whether all of it is code."""

    line: int
    text: str
    code: bool

    def spans(self) -> list[tuple[int, str]]:
        """Each piece of code in the unit and where it starts in the text."""
        if self.code:
            return [(0, self.text)]
        return [(match.start(2), match.group(2).strip()) for match in CODE_SPAN.finditer(self.text)]


@dataclass(frozen=True)
class Passage:
    document: str
    unit: Unit
    name: Name
    at: int  # where in the unit's text the name matched


def _diff_path(line: str) -> str | None:
    """The repository path a `+++ b/<path>` or `--- a/<path>` line names, or None for /dev/null."""
    value = line[4:].rstrip("\n")
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value == "/dev/null":
        return None
    return value[2:] if value[:2] in {"a/", "b/"} else value


def read_diff(text: str) -> tuple[list[str], list[Hunk], list[tuple[str, str, Hunk]]]:
    """The changed paths in diff order, their hunks, and each top-level definition a hunk adds, removes, or changes
    inside, as its keyword, its name, and that hunk."""
    paths: list[str] = []
    hunks: list[Hunk] = []
    definitions: list[tuple[str, str, Hunk]] = []
    old: str | None = None
    path: str | None = None
    current: Hunk | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            old = path = current = None
        elif line.startswith("--- ") and current is None:
            old = _diff_path(line)
        elif line.startswith("+++ ") and current is None:
            path = _diff_path(line) or old
            if path is not None and path not in paths:
                paths.append(path)
        elif (match := HUNK.match(line)) and path is not None:
            first = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            current = Hunk(path, line.split(" @@", 1)[0] + " @@", first, first + max(count, 1) - 1)
            hunks.append(current)
            context = DEFINITION.match(match.group(3))
            if context:
                definitions.append((context.group(1), context.group(2), current))
        elif current is not None and line[:1] in {"+", "-"}:
            definition = DEFINITION.match(line[1:])
            if definition:
                definitions.append((definition.group(1), definition.group(2), current))
    return paths, hunks, definitions


def _counts(text: str) -> bool:
    return len(text) >= MIN_NAME_LENGTH and text not in IGNORED_NAMES and not text.startswith("__")


def _is_test(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.rsplit(".", 1)[0].endswith("_test")


def names_to_find(diff: str) -> list[Name]:
    """Each name a document may use for what the diff changes, once, with the hunk it relates to, in the order a
    passage is matched: the functions and classes, then the script file names, so a path names its file, then the
    modules. A test file's names are left out: a document names a test as what holds a rule, which validation checks.
    """
    paths, hunks, definitions = read_diff(diff)
    first_hunk: dict[str, Hunk] = {}
    for hunk in hunks:
        first_hunk.setdefault(hunk.path, hunk)
    names: dict[str, Name] = {}
    for keyword, text, hunk in definitions:
        if hunk.path.endswith(".py") and not _is_test(hunk.path) and _counts(text):
            names.setdefault(text, Name(text, "class" if keyword == "class" else "function", hunk))
    scripts = [path for path in paths if path.endswith(SCRIPT_SUFFIXES) and not _is_test(path)]
    for kind in ("script", "module"):
        for path in scripts:
            base = path.rsplit("/", 1)[-1]
            text = base if kind == "script" else base[:-3]
            if kind == "script" or (path.endswith(".py") and _counts(text)):
                names.setdefault(text, Name(text, kind, first_hunk.get(path, Hunk(path, "(no hunk)", 0, 0))))
    return list(names.values())


def documents(root: Path) -> list[tuple[str, Path]]:
    """Each document the snapshot holds that the route searches, by its repository path, in path order."""
    found: dict[str, Path] = {}
    for pattern in DOCUMENTS:
        for path in root.glob(pattern):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and relative not in RECORDS:
                found[relative] = path
    return sorted(found.items())


def units(text: str) -> Iterator[Unit]:
    """A document's passages: each heading, table row, list item, paragraph, and fenced block, with its first line."""
    block: list[str] = []
    start = 0
    fence: str | None = None
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        opening = FENCE.match(line)
        if fence is not None:
            if opening and opening.group(1) == fence:
                if block:
                    yield Unit(start, "\n".join(block), True)
                block, fence = [], None
            else:
                block.append(line)
            continue
        single = line.lstrip().startswith(("#", "|"))
        if opening or not line.strip() or single or LIST_ITEM.match(line):
            if block:
                yield Unit(start, "\n".join(block).strip(), False)
            block = []
        if opening:
            start, fence = number + 1, opening.group(1)
        elif single:
            if not TABLE_SEPARATOR.match(line):
                yield Unit(number, line.strip(), False)
        elif line.strip():
            if not block:
                start = number
            block.append(line)
    if block:
        yield Unit(start, "\n".join(block).strip(), fence is not None)


def passages(root: Path, names: list[Name]) -> list[Passage]:
    """Each unit of each document that names one of `names` in code, by the first of them it names, taken in turn
    from each name, the names fewest passages name first, so a script every command example names fills no bound
    that a changed function's one passage needs."""
    patterns = [(name, name.pattern()) for name in names]
    by_name: dict[str, list[Passage]] = {name.text: [] for name in names}
    for relative, path in documents(root):
        for unit in units(path.read_text(encoding="utf-8", errors="replace")):
            match = next(
                (
                    Passage(relative, unit, name, offset + hit.start())
                    for name, pattern in patterns
                    for offset, span in unit.spans()
                    for hit in [pattern.search(span)]
                    if hit
                ),
                None,
            )
            if match:
                by_name[match.name.text].append(match)
    found = sorted((items for items in by_name.values() if items), key=len)
    width = max((len(items) for items in found), default=0)
    return [items[index] for index in range(width) for items in found if index < len(items)]


def excerpt(passage: Passage) -> str:
    """The passage's text, or, past MAX_PASSAGE_CHARACTERS, that many characters around where the name matched."""
    text = passage.unit.text
    if len(text) <= MAX_PASSAGE_CHARACTERS:
        return text
    start = max(0, min(passage.at - MAX_PASSAGE_CHARACTERS // 2, len(text) - MAX_PASSAGE_CHARACTERS))
    end = start + MAX_PASSAGE_CHARACTERS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def render(found: list[Passage]) -> str:
    """The file the route's reviewer reads: each passage given, then how many past the bound were not."""
    blocks = []
    for passage in found[:MAX_PASSAGES]:
        name, hunk = passage.name, passage.name.hunk
        blocks.append(
            f"PASSAGE {passage.document}:{passage.unit.line} names {name.text} ({name.kind} in {hunk.path}), "
            f"hunk {hunk.header} at new lines {hunk.first}-{hunk.last}\n{excerpt(passage)}\n"
        )
    if len(found) > MAX_PASSAGES:
        blocks.append(f"OMITTED {len(found) - MAX_PASSAGES} more passages past the first {MAX_PASSAGES}\n")
    return "\n".join(blocks)


def _run() -> int:
    parser = argparse.ArgumentParser(description="Find the document passages that name what a pull request changes.")
    parser.add_argument("--source-root", required=True, type=Path)
    arguments = parser.parse_args()
    missing = [variable for variable in (DIFF_VARIABLE, OUTPUT_VARIABLE) if not os.environ.get(variable)]
    if missing:
        print(f"FAILED {' and '.join(missing)} not set; a review sets them for a condition")
        return 2
    diff = Path(os.environ[DIFF_VARIABLE]).read_bytes().decode("utf-8", errors="replace")
    found = passages(arguments.source_root, names_to_find(diff))
    print(f"PASSAGES {len(found)} given={min(len(found), MAX_PASSAGES)}")
    if not found:
        return 1
    Path(os.environ[OUTPUT_VARIABLE]).write_bytes(render(found).encode("utf-8"))
    return 0


def main() -> int:
    try:
        return _run()
    except Exception as error:
        # Exit 1 would close the route, so a failure run_main would report that way exits 2 and fails the review.
        print(failed_line(error))
        return 2


if __name__ == "__main__":
    run_main(main, newline="\n")

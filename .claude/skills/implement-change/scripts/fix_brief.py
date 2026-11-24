"""Write the brief a fix is planned from: what the issue names, the callers of each symbol it names, each file's twins,
the lessons patterns the files meet, the contract entries that name them, and the rules every fix session meets, so the
session considers each before it writes code.

    fix_brief.py <issue> [--path <path> ...]

The issue is read with `gh issue view`, its body and its comments alike, through ejryan89-skill-core's GitHubClient.
What it names is read from its inline code spans, through markdown_sections' `prose_code_spans`, never its prose:

- a span that names a tracked file, by its path from the top of the repository, or by a name only one tracked file
  has, is one of the brief's files; a line, a heading, or a test after it, as in `<path>:<line>`, `<path>#<heading>`,
  or `<path>::<test>`, is left off. A name several tracked files have is listed for the author to choose with `--path`.
- a span written as code, an identifier holding `_`, a capital past its first letter, a `.` or `:` between
  identifiers, or ending in `()`, is a symbol, its last identifier searched for, unless that identifier is shorter than
  three characters or is the extension of a tracked file, as in `lessons.md`.

Each `--path` is a file the change edits or adds, written from the top of the repository, added to the issue's.

The brief, a Markdown file written to a new private folder under the temporary directory, holds:

- the files, each named by the issue or by `--path`, or not there yet;
- the construct and its spellings, for the author to name, as the sibling-spellings reviewer will;
- for each symbol, the tracked files holding it as a whole word, as `git grep -w -F` matches one, with their line
  numbers, each file read through lessons_patterns' `Corpus`, which judges its name inside the repository first;
- for each file, its twin in the other shell, `<name>.sh` beside `<name>.ps1`, each other tracked file of its name in
  a folder of its folder's name, such as a skill's shim, and, when no more than MAX_TWINS have its name, the rest;
- the lessons patterns the files meet, read through implement-change's lessons_patterns `listing`, with the forms it
  could not search and the files it could not read; the catalogue, `docs/lessons.md`, holds every pattern's forms,
  so it is never searched for them;
- each code span of the implementation profile's *Contract files* section, `docs/implementing-changes.md`, that names
  one of the files or a folder holding it;
- the rules every fix session meets, the text of `references/brief.md` beside this script's folder.

Each list is bounded, and a list cut short says how many it left out and the command that lists them all: at most
MAX_PATHS files the issue names, MAX_SYMBOLS symbols, MAX_CALLER_FILES files for each symbol with MAX_CALLER_LINES line
numbers each, MAX_TWINS twins for each file, and MAX_EVIDENCE of what meets each pattern.

Limits: a file or a symbol the issue names in its prose, outside a code span, is not read; a folder the issue names is
no file; a symbol's callers are the files that hold its name as a word, which counts a definition, a comment, and a
different name spelled the same, and misses a call through another name; a tracked file that is refused inside the
repository, not there, or not UTF-8 holds no caller.

Every line is one fact, its fields tab-separated:

    ISSUE <number> <title>          the issue read
    BRIEF "<file>"                  the brief written: read it, and complete its construct section
    FAILED <reason>                 the issue, the repository's files, the catalogue, or the profile could not be read,
                                    a path is refused, or the brief could not be written

Exit status:
  0  the brief was written
  1  a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import file_presence
import lessons_patterns
import private_folder
import whole_number
from console import run_main
from flat_text import fact_line, flat_text
from git_client import GitClient, GitError
from github_client import GitHubClient, GitHubError, subprocess_runner
from lessons_catalogue import CATALOGUE, CatalogueError
from markdown_sections import code_block_lines, code_spans, prose_code_spans
from path_containment import posix_within
from text_lines import text_lines

# The checkout this script belongs to, whose files the brief names.
REPOSITORY = Path(__file__).resolve().parents[4]
# The rules every fix session meets, which the brief carries as written.
RULES = Path(__file__).resolve().parents[1] / "references" / "brief.md"
# The implementation profile and its section of the files whose change is a contract change.
PROFILE = "docs/implementing-changes.md"
CONTRACT_HEADING = "## Contract files"
# The reviewer whose catalogue of spellings the author's construct section answers.
SPELLINGS_REVIEWER = ".claude/skills/review-repository/references/sibling-spellings.md"
# The bounds of each list the brief holds.
MAX_PATHS = 20
MAX_SYMBOLS = 12
MAX_CALLER_FILES = 10
MAX_CALLER_LINES = 5
MAX_TWINS = 5
MAX_EVIDENCE = 8
# The shortest identifier searched for as a symbol.
MIN_SYMBOL = 3
# The issue's fields the brief reads.
ISSUE_FIELDS = "number,title,url,body,comments"
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
# A span written as code: identifiers joined by `.` or `:`, as in `module:name` or `Class.member`, maybe called.
SYMBOL = re.compile(rf"{IDENTIFIER}(?:[.:]{IDENTIFIER})*(?:\(\))?")
# A place inside a file a span may cite after its path: a test, a heading, or a line or a range of lines.
LOCATION = re.compile(r"::.*$|#.*$|:[0-9]+(?:[-:][0-9]+)?$")
# Each shell's twin, the extension of the script that does the same work in the other shell.
SHELL_TWINS = {".sh": ".ps1", ".ps1": ".sh"}
# A character of a word, as `git grep -w` reads one.
WORD_CHARACTER = "[A-Za-z0-9_]"


class BriefError(Exception):
    """The brief cannot be written: its message says why."""


@dataclass(frozen=True)
class Issue:
    """An issue as the brief reads it: its number, title, address, and the text of its body and each comment."""

    number: int
    title: str
    url: str
    texts: tuple[str, ...]


@dataclass(frozen=True)
class Named:
    """What an issue's code spans name: the tracked files, each name several tracked files hold with those files, and
    the symbols, each in the order the issue first names it, and how many files or symbols the bounds left out."""

    paths: list[str]
    ambiguous: dict[str, list[str]]
    symbols: list[str]
    paths_cut: int
    symbols_cut: int


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BriefError(f"the issue's {field} is not text")
    return value


def read_issue(github: GitHubClient, number: int) -> Issue:
    """The issue `number`, from `gh issue view`; BriefError when gh cannot read it or answers in a shape not an
    issue's."""
    try:
        payload = github.json(["issue", "view", str(number), "--json", ISSUE_FIELDS])
    except GitHubError as error:
        raise BriefError(f"cannot read issue #{number}: {flat_text(str(error))}") from error
    if not isinstance(payload, dict) or payload.get("number") != number:
        raise BriefError(f"gh answered issue #{number} with something not that issue")
    comments = payload.get("comments")
    if not isinstance(comments, list) or not all(isinstance(comment, dict) for comment in comments):
        raise BriefError("the issue's comments are not a list of comments")
    texts = [_text(payload.get("body"), "body"), *(_text(comment.get("body"), "comment") for comment in comments)]
    return Issue(number, _text(payload.get("title"), "title"), _text(payload.get("url"), "url"), tuple(texts))


def issue_spans(issue: Issue) -> list[str]:
    """Every inline code span of the issue's body and comments outside a code block, stripped, once each, in the order
    the issue first holds it."""
    spans = (span.strip() for text in issue.texts for _, span in prose_code_spans(text_lines(text)))
    return list(dict.fromkeys(span for span in spans if span))


def by_name(tracked: Sequence[str]) -> dict[str, list[str]]:
    """The tracked files, by their file name."""
    names: dict[str, list[str]] = {}
    for name in tracked:
        names.setdefault(PurePosixPath(name).name, []).append(name)
    return names


def path_of(span: str, tracked: frozenset[str], names: dict[str, list[str]]) -> list[str]:
    """The tracked files a span names: the one at its path, or every file of its name when it is a name alone; none
    when it names no tracked file."""
    name = LOCATION.sub("", span).removeprefix("./")
    if name in tracked:
        return [name]
    return [] if "/" in name else names.get(name, [])


def symbol_of(span: str, extensions: frozenset[str]) -> str | None:
    """The identifier a span written as code names, its last; None for a span that is not written as code, a name
    too short to search, or a file name."""
    if not SYMBOL.fullmatch(span):
        return None
    bare = span.removesuffix("()")
    name = re.split(r"[.:]", bare)[-1]
    written_as_code = (
        span.endswith("()") or "_" in bare or "." in bare or ":" in bare or any(c.isupper() for c in bare[1:])
    )
    if not written_as_code or len(name) < MIN_SYMBOL or f".{name}" in extensions:
        return None
    return name


def named(issue: Issue, tracked: Sequence[str]) -> Named:
    """What the issue's code spans name, within the bounds."""
    tracked_set = frozenset(tracked)
    names = by_name(tracked)
    extensions = frozenset(PurePosixPath(name).suffix for name in tracked if PurePosixPath(name).suffix)
    paths: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    symbols: list[str] = []
    for span in issue_spans(issue):
        files = path_of(span, tracked_set, names)
        if len(files) == 1:
            paths.append(files[0])
        elif files:
            ambiguous[span] = sorted(files)
        else:
            symbol = symbol_of(span, extensions)
            if symbol is not None:
                symbols.append(symbol)
    paths, symbols = list(dict.fromkeys(paths)), list(dict.fromkeys(symbols))
    return Named(
        paths[:MAX_PATHS],
        ambiguous,
        symbols[:MAX_SYMBOLS],
        max(0, len(paths) - MAX_PATHS),
        max(0, len(symbols) - MAX_SYMBOLS),
    )


def holders(corpus: lessons_patterns.Corpus, symbol: str) -> dict[str, list[int]]:
    """Each tracked file holding `symbol` as a whole word, as `git grep -w` reads a word, with the numbers of the lines
    it is on; the OSError of a file that cannot be examined or read."""
    word = re.compile(rf"(?<!{WORD_CHARACTER}){re.escape(symbol)}(?!{WORD_CHARACTER})")
    found: dict[str, list[int]] = {}
    for name in corpus.every_name():
        text = corpus.text(name)
        if text is None or symbol not in text:
            continue
        numbers = [index + 1 for index, line in enumerate(text_lines(text)) if word.search(line)]
        if numbers:
            found[name] = numbers
    return found


def twins(path: str, tracked: frozenset[str], names: dict[str, list[str]]) -> tuple[list[str], int]:
    """The tracked files that may hold the same construct as `path`, and how many other files of its name are left
    out: its twin in the other shell, then each other file of its name in a folder of its folder's name, such as a
    skill's shim, then, when no more than MAX_TWINS other files have its name, each of those."""
    file = PurePosixPath(path)
    other = SHELL_TWINS.get(file.suffix)
    shell = [file.with_suffix(other).as_posix()] if other else []
    same = sorted(set(names.get(file.name, [])) - {path})
    near = [name for name in same if PurePosixPath(name).parent.name == file.parent.name]
    rest = [name for name in same if name not in near]
    if len(same) > MAX_TWINS:
        return [name for name in shell if name in tracked] + near, len(rest)
    return [name for name in shell if name in tracked] + near + rest, 0


def contract_entries(root: Path, paths: Sequence[str]) -> list[tuple[int, str, str]] | None:
    """Each code span of the profile's Contract files section that names one of `paths`, or a folder holding one, as
    its line number, the span, and the path; None when the repository has no profile or no such section."""
    profile = root / PROFILE
    try:
        if not file_presence.is_present(profile):
            return None
        lines = text_lines(profile.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, file_presence.NotAFile) as error:
        raise BriefError(f"cannot read {PROFILE}: {flat_text(str(error))}") from error
    fenced = code_block_lines(lines)
    prose = [index for index in range(len(lines)) if index not in fenced]
    start = next((index for index in prose if lines[index].rstrip() == CONTRACT_HEADING), None)
    if start is None:
        return None
    end = next((index for index in prose if index > start and lines[index].startswith("## ")), len(lines))
    spans = [(index, span.text.strip()) for index in prose if start < index < end for span in code_spans(lines[index])]
    return [
        (index + 1, place, path)
        for index, place in spans
        for path in paths
        if path == place or (place.endswith("/") and posix_within(path, place.rstrip("/")))
    ]


def code(text: str) -> str:
    """`text` as one inline code span on one line, its backticks kept."""
    flat = flat_text(text)
    return f"`` {flat} ``" if "`" in flat else f"`{flat}`"


def _files_section(named_files: Named, given: Sequence[str], tracked: frozenset[str]) -> list[str]:
    lines = ["## Files", "", *(f"- {code(path)}: named by the issue" for path in named_files.paths)]
    lines += [
        f"- {code(path)}: given with `--path`" + ("" if path in tracked else ", not there yet")
        for path in given
        if path not in named_files.paths
    ]
    if named_files.paths_cut:
        lines.append(f"- and {named_files.paths_cut} more the issue names past the first {MAX_PATHS}")
    for span, files in named_files.ambiguous.items():
        choices = ", ".join(code(name) for name in files[:MAX_TWINS])
        more = f", and {len(files) - MAX_TWINS} more" if len(files) > MAX_TWINS else ""
        lines.append(
            f"- {code(span)} names {len(files)} tracked files: {choices}{more}; give the one meant with `--path`"
        )
    if len(lines) == 2:
        lines.append(
            "The issue names no tracked file in a code span: give each file the change will touch with `--path` and "
            "write the brief again."
        )
    return lines


def _construct_section() -> list[str]:
    return [
        "## Construct and its spellings (to complete)",
        "",
        "Name the construct the issue's rule is about and the catalogue its spellings come from, such as builtins, "
        "options and their bundled forms, binding modes, import forms, error kinds, exit codes, keys, characters, "
        f"path spellings, or shells, as the sibling-spellings reviewer, {code(SPELLINGS_REVIEWER)}, will. Then "
        "decide, for each entry, whether the change handles it, and name what it leaves in the pull request.",
    ]


def _callers_section(found: dict[str, dict[str, list[int]]], cut: int) -> list[str]:
    lines = ["## Callers"]
    if not found:
        lines += ["", "The issue names no symbol in a code span."]
    for symbol, files in found.items():
        search = code(f"git grep -n -w -F -e '{symbol}'")
        lines += ["", f"### {code(symbol)}", "", f"{len(files)} tracked files hold it as a word; {search} lists them."]
        if files:
            lines.append("")
        for name in sorted(files)[:MAX_CALLER_FILES]:
            numbers = files[name]
            shown = ", ".join(str(number) for number in numbers[:MAX_CALLER_LINES])
            more = f", and {len(numbers) - MAX_CALLER_LINES} more" if len(numbers) > MAX_CALLER_LINES else ""
            lines.append(f"- {code(name)}: {shown}{more}")
        if len(files) > MAX_CALLER_FILES:
            lines.append(f"- and {len(files) - MAX_CALLER_FILES} more files")
    if cut:
        lines += ["", f"The issue names {cut} more symbols past the first {MAX_SYMBOLS}; search for each by hand."]
    return lines


def _twins_section(paths: Sequence[str], tracked: frozenset[str], names: dict[str, list[str]]) -> list[str]:
    lines = ["## Twins", ""]
    for path in paths:
        found, common = twins(path, tracked, names)
        listed = [code(name) for name in found[:MAX_TWINS]]
        if len(found) > MAX_TWINS:
            listed.append(f"and {len(found) - MAX_TWINS} more")
        if common:
            listed.append(f"and {common} other tracked files of its name, too common to list")
        if listed:
            lines.append(f"- {code(path)}: " + ", ".join(listed))
    if len(lines) == 2:
        lines.append("No file has a twin in the other shell or another tracked file of its name.")
    return lines


def _lessons_section(facts: Sequence[tuple[str, ...]], files: Sequence[str]) -> list[str]:
    lines = ["## Lessons patterns", ""]
    if CATALOGUE in files:
        lines += [f"{code(CATALOGUE)} holds every pattern's forms, so it is not searched for them.", ""]
    for key, *fields in facts:
        if key == "PATTERN":
            place, pattern, *evidence = fields
            shown = [code(item) for item in evidence[:MAX_EVIDENCE]]
            if len(evidence) > MAX_EVIDENCE:
                shown.append(f"and {len(evidence) - MAX_EVIDENCE} more")
            lines.append(f"- {code(place)} {flat_text(pattern)}: " + "; ".join(shown))
        elif key == "UNSEARCHED":
            place, pattern, form, why = fields
            lines.append(f"- {code(place)} {flat_text(pattern)}: search for {code(form)} by hand, {flat_text(why)}")
        elif key == "UNREAD":
            lines.append(f"- {code(fields[0])} could not be read, {flat_text(fields[1])}: search it by hand")
    if not any(key in {"PATTERN", "UNSEARCHED", "UNREAD"} for key, *_ in facts):
        lines.append("No pattern of the catalogue meets these files.")
    else:
        lines += [
            "",
            "Read each pattern from its line: follow its rule, call the helper its **Use** names, and run its "
            "**Search for** across the repository, fixing or filing each hit.",
        ]
    return lines


def _contract_section(entries: list[tuple[int, str, str]] | None) -> list[str]:
    lines = ["## Contract", ""]
    if entries is None:
        return [*lines, f"The repository has no *Contract files* section in {code(PROFILE)}."]
    for number, place, path in entries:
        lines.append(f"- {code(f'{PROFILE}:{number}')} names {code(place)}, which holds {code(path)}")
    if not entries:
        lines.append(f"No *Contract files* entry of {code(PROFILE)} names these files.")
    return lines


def brief_text(issue: Issue, sections: Sequence[list[str]], rules: str) -> str:
    """The brief: its heading, how to read it, its sections, and the rules every fix session meets."""
    head = [
        f"# Fix brief for #{issue.number}: {flat_text(issue.title)}",
        "",
        flat_text(issue.url),
        "",
        f"Read the issue and its comments first: {code(f'gh issue view {issue.number} --comments')}. This brief "
        "gathers what the change must consider; you judge each entry. Carry what applies into the plan's handoff "
        "prompt, and into the pull request's `## Forms considered` section or its `Siblings:` line.",
    ]
    blocks = [head, *sections]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n\n" + rules.rstrip("\n") + "\n"


def write_brief(
    root: Path, github: GitHubClient, git: GitClient, number: int, given: Sequence[str]
) -> tuple[Issue, Path]:
    """The issue read and the brief written for it; BriefError, GitError, CatalogueError, lessons_patterns' Refused,
    or an OSError when one of them cannot be."""
    issue = read_issue(github, number)
    tracked = git.names(["ls-files"], directory=root)
    tracked_set = frozenset(tracked)
    names = by_name(tracked)
    found = named(issue, tracked)
    files = list(dict.fromkeys([*found.paths, *given]))
    corpus = lessons_patterns.Corpus(root, (), lambda: tracked)
    callers = {symbol: holders(corpus, symbol) for symbol in found.symbols}
    facts = lessons_patterns.listing(root, [name for name in files if name != CATALOGUE], lambda: tracked)
    sections = [
        _files_section(found, given, tracked_set),
        _construct_section(),
        _callers_section(callers, found.symbols_cut),
        _twins_section(files, tracked_set, names),
        _lessons_section(facts, files),
        _contract_section(contract_entries(root, files)),
    ]
    rules = RULES.read_text(encoding="utf-8")
    folder = Path(tempfile.mkdtemp(prefix="implement-change-brief-", dir=private_folder.temporary_directory()))
    brief = folder / f"brief-{issue.number}.md"
    brief.write_text(brief_text(issue, sections, rules), encoding="utf-8", newline="\n")
    return issue, brief


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = REPOSITORY,
    github: GitHubClient | None = None,
    git: GitClient | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Write the brief a fix is planned from, from its issue.")
    parser.add_argument("issue", type=whole_number.argument(1), help="the issue the change resolves")
    parser.add_argument(
        "--path", action="append", default=[], help="a file the change edits or adds, from the top (repeatable)"
    )
    options = parser.parse_args(argv)
    client = github or GitHubClient(runner=partial(subprocess_runner, cwd=root))
    try:
        issue, brief = write_brief(root, client, git or GitClient(), options.issue, options.path)
    except (BriefError, CatalogueError, lessons_patterns.Refused) as error:
        print(fact_line("FAILED", error))
        return 1
    except GitError as error:
        print(fact_line("FAILED", f"git cannot list the repository's files: {error}"))
        return 1
    except OSError as error:
        print(fact_line("FAILED", f"cannot read a file or write the brief: {error}"))
        return 1
    print(fact_line("ISSUE", issue.number, issue.title))
    print(fact_line("BRIEF", f'"{brief}"'))
    return 0


if __name__ == "__main__":
    run_main(main)

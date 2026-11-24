"""List the patterns of the lessons catalogue, docs/lessons.md, that the files a change will touch meet, so the
change is planned and written knowing each rule, its helper, and its guard.

    lessons_patterns.py <path> [<path> ...]

Each path is a file the change will edit or add, written from the top of the repository, such as `tools/x.py`. The
catalogue is read through ejryan89-skill-core's lessons_catalogue, the reader validation's ledger check holds it to,
and the patterns are listed in the order it holds them. A pattern meets a file in two ways:

- its **Use** names a mechanism, `<module>:<name>`, whose module is the file itself, as it is imported from the
  repository root or from a skill's `scripts` (lessons_catalogue's `mechanism_bases`), or a module the file's Python
  imports, by an `import` or a `from` statement, read through ejryan89-skill-core's python_reader;
- one of its **Search for** forms occurs in the file. A form that is an `rg` command, read by Bash's grammar through
  ejryan89-skill-core's bash_syntax, is searched as rg searches: its pattern line by line, or across lines with `-U`,
  inside the paths and `-g` globs it names, then through each `rg` after a `|`, which keeps the lines it matches, or,
  with `-v`, those it does not. Any other form is searched as its text, each `<placeholder>` and `...` in it standing
  for any text on the same line.

A file that is not there yet is met through its own module alone. The lister writes nothing, and reads only the
catalogue and the paths given, each judged inside the repository by path_containment's `refusal` before it is opened.

The limits, each a form the session reads itself, which its pattern's prose places:
- a form that is one word of letters, digits, and underscores, such as `never` or `OSError`, is not searched, since
  it occurs in most files of its language, and neither is a form that is nothing but placeholders;
- an `rg` pattern is compiled by Python's `re`, which reads the patterns the catalogue writes as rg's regular
  expressions do; one it cannot compile, an rg option the lister does not read, and a command after a `|` that is not
  `rg` are each listed as UNSEARCHED;
- a relative import, `from . import x`, names no module the catalogue's Use reads, and is not matched.

This is the gathering a later fix brief (EJRyan89/coding-agent-skills#2177) calls through `listing` for the lessons
pattern of the files an issue names.

Every line is one fact, its fields tab-separated:

    USE <catalogue>:<line> <pattern> <path> <module>      the pattern's Use, at that line, names a module the file is
                                                          or imports
    SEARCH <catalogue>:<line> <pattern> <path>:<line> <form>
                                                          a Search for form, at that catalogue line, occurs first at
                                                          that line of the file
    UNSEARCHED <catalogue>:<line> <pattern> <form> <why>  a form the lister could not search: search for it by hand
    NEW <path>                                            nothing is at the path yet, so only its module is matched
    UNREAD <path> <reason>                                a file that is not UTF-8 text, or Python that does not
                                                          parse: the forms or imports it holds are not matched
    FAILED <reason>                                       the catalogue could not be read, a path is refused or is
                                                          not a file, or a file could not be examined or read

A file no pattern meets prints nothing.

Exit status:
  0  the patterns were listed, none included
  1  a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_syntax
import file_presence
import lessons_catalogue
import python_reader
from console import run_main
from flat_text import fact_line
from lessons_catalogue import CATALOGUE, CatalogueError, Pattern
from path_containment import posix_within, refusal
from text_lines import text_lines

# One line's fact: its key, such as USE, then its fields, which main prints through fact_line.
type Fact = tuple[str, ...]
# The checkout this script belongs to, whose catalogue it reads and whose files the paths name.
REPOSITORY = Path(__file__).resolve().parents[4]
# A placeholder in a form, `<name>` or `...`, which stands for any text on the line.
PLACEHOLDER = re.compile(r"<[^<>\s]+>|\.\.\.")
# A form that names words alone, such as `never` or `is not None`, with no code around them.
WORDS_ONLY = re.compile(r"[\w ]*")
# A form that names a file or a folder, such as `tools/` or `SKILL.md`: where a violation matters, not its form.
PATH_FORM = re.compile(r"[\w.-]*(/[\w.-]*)+|[\w-]+\.[A-Za-z]{1,4}")
# A word of three or more characters, which a form holds to be more than punctuation, such as `..` or `-c`.
WORD = re.compile(r"\w{3,}")
# The rg options whose value is the next word when the option ends its word.
RG_VALUED = frozenset("gAeBC")
# The rg short options read: line numbers, quiet, and context, which change no match; across lines; inverted.
RG_FLAGS = frozenset("nqUv")
RG_LONG = {"--glob": "g", "--multiline": "U", "--invert-match": "v", "--line-number": "n", "--regexp": "e"}


class Unsearchable(ValueError):
    """A form the lister cannot search, with the reason."""


class Refused(Exception):
    """A path the lister does not read, with the reason."""


@dataclass(frozen=True)
class Target:
    """A path the change touches: its name from the top of the repository, its text, or None when nothing is there
    yet or it cannot be read, and the modules it is or imports."""

    name: str
    text: str | None
    modules: frozenset[str]


@dataclass(frozen=True)
class RgSearch:
    """One `rg` of a form: its pattern, whether it reads across lines or keeps the lines it does not match, and the
    paths and globs it is held to."""

    pattern: re.Pattern[str]
    multiline: bool
    invert: bool
    scopes: tuple[str, ...]
    globs: tuple[str, ...]


def _short_options(word: str, words: Iterator[str], options: dict[str, list[str]]) -> None:
    """Read one word of rg's short options into `options`, a valued option taking the rest of its word or the next."""
    for index, letter in enumerate(word[1:], start=1):
        if letter in RG_VALUED:
            options.setdefault(letter, []).append(word[index + 1 :] or next(words, ""))
            return
        if letter not in RG_FLAGS:
            raise Unsearchable(f"the rg option -{letter} is not read")
        options.setdefault(letter, []).append("")


def _rg_search(words: list[str]) -> RgSearch | None:
    """The search one `rg` command's words, from its name, ask for; None when they name no pattern."""
    options: dict[str, list[str]] = {}
    positional: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if word == "--":
            positional += list(rest)
        elif word.startswith("--"):
            name, equals, value = word.partition("=")
            if name not in RG_LONG:
                raise Unsearchable(f"the rg option {name} is not read")
            letter = RG_LONG[name]
            options.setdefault(letter, []).append(value if equals or letter not in RG_VALUED else next(rest, ""))
        elif word.startswith("-") and word != "-":
            _short_options(word, rest, options)
        else:
            positional.append(word)
    if options.get("e"):
        positional.insert(0, options["e"][0])
    if not positional:
        return None
    try:
        pattern = re.compile(positional[0], re.MULTILINE)
    except re.error as error:
        raise Unsearchable(f"Python's re cannot compile the pattern: {error}") from None
    return RgSearch(
        pattern,
        "U" in options,
        "v" in options,
        tuple(scope.rstrip("/") for scope in positional[1:]),
        tuple(options.get("g", [])),
    )


def rg_searches(form: str) -> list[RgSearch] | None:
    """The searches a form that is an `rg` command runs, the first and then each `rg` after a `|`; None for a form that
    is not an `rg` command naming a pattern, which is searched as its text."""
    try:
        commands = [bash_syntax.command_words(command) for command in bash_syntax.simple_commands(form)]
    except bash_syntax.BashSyntaxError:
        return None
    if not commands or not commands[0] or commands[0][0] != "rg":
        return None
    first = _rg_search(commands[0])
    if first is None:
        return None
    searches = [first]
    for words in commands[1:]:
        later = _rg_search(words) if words and words[0] == "rg" else None
        if later is None:
            raise Unsearchable("a command after the first is not an rg naming a pattern")
        searches.append(later)
    return searches


def text_search(form: str) -> re.Pattern[str] | None:
    """The search a form that is text asks for, each placeholder standing for any text on the line; None for a form
    too common, or too empty, to tell a file anything."""
    pieces = PLACEHOLDER.split(form)
    fixed = "".join(pieces).strip()
    if WORDS_ONLY.fullmatch(fixed) or PATH_FORM.fullmatch(fixed) or not WORD.search(fixed):
        return None
    return re.compile(".*?".join(re.escape(piece) for piece in pieces))


def _held(search: RgSearch, name: str) -> bool:
    """Whether the paths and globs of an `rg` hold the file `name`."""
    if search.scopes and not any(scope in {"", "."} or posix_within(name, scope) for scope in search.scopes):
        return False
    base = PurePosixPath(name).name
    return not search.globs or any(
        fnmatch.fnmatchcase(base if "/" not in glob else name, glob) for glob in search.globs
    )


def rg_first_line(searches: list[RgSearch], name: str, text: str) -> int | None:
    """The first line of `text` the `rg` searches of a form report for the file `name`, or None."""
    first, filters = searches[0], searches[1:]
    if not _held(first, name):
        return None
    lines = text_lines(text)
    if first.multiline:
        # Each line's offset in the text, from text_lines' own breaks, so a match is numbered as its line is.
        offsets = list(accumulate((len(line) for line in text_lines(text, keepends=True)), initial=0))
        found = sorted({bisect_right(offsets, match.start()) - 1 for match in first.pattern.finditer(text)})
    else:
        found = [index for index, line in enumerate(lines) if bool(first.pattern.search(line)) != first.invert]
    for index in found:
        line = lines[index] if index < len(lines) else ""
        if all(bool(search.pattern.search(line)) != search.invert for search in filters):
            return index + 1
    return None


def form_line(form: str, name: str, text: str) -> int | None:
    """The first line of the file `name`, whose text is `text`, at which `form` occurs, or None; Unsearchable for a
    form the lister cannot search."""
    searches = rg_searches(form)
    if searches is not None:
        return rg_first_line(searches, name, text)
    pattern = text_search(form)
    if pattern is None:
        return None
    return next((number for number, line in enumerate(text_lines(text), start=1) if pattern.search(line)), None)


def own_modules(root: Path, name: str) -> set[str]:
    """The modules the Python file `name` is, as imported from each folder a mechanism's module may be imported from."""
    if not name.endswith(".py"):
        return set()
    modules: set[str] = set()
    for base in lessons_catalogue.mechanism_bases(root):
        folder = "" if base == root else base.relative_to(root).as_posix()
        if folder and not posix_within(name, folder, strict=True):
            continue
        parts = list(PurePosixPath(name[len(folder) + 1 if folder else 0 :]).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() for part in parts):
            modules.add(".".join(parts))
    return modules


def imported_modules(source: str) -> set[str]:
    """Every module a Python source imports by name: each `import`'s module and the packages above it, and each
    absolute `from`'s module and that module's member it names, which may be a module too."""
    modules: set[str] = set()
    for node in ast.walk(python_reader.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                modules |= {".".join(parts[:end]) for end in range(1, len(parts) + 1)}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules |= {f"{node.module}.{alias.name}" for alias in node.names}
    return modules


def read_target(root: Path, name: str, lines: list[Fact]) -> Target:
    """The path `name` as the lister reads it, appending a NEW or UNREAD line it owes to `lines`; Refused for a path
    refused inside the repository or one that is not a file."""
    reason = refusal(name, root)
    if reason is not None:
        raise Refused(f"{name} is refused: {reason}")
    name = PurePosixPath(name.replace("\\", "/")).as_posix()
    modules = own_modules(root, name)
    path = root / name
    try:
        present = file_presence.is_present(path)
    except file_presence.NotAFile:
        raise Refused(f"{name} is not a file") from None
    if not present:
        lines.append(("NEW", name))
        return Target(name, None, frozenset(modules))
    # A file that cannot be opened or read raises, never read as one with nothing in it; one that is not UTF-8 text,
    # such as an image, is named and met through its module alone.
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        lines.append(("UNREAD", name, str(error)))
        return Target(name, None, frozenset(modules))
    if name.endswith(".py"):
        try:
            modules |= imported_modules(text)
        except python_reader.PythonRefused as error:
            lines.append(("UNREAD", name, str(error)))
    return Target(name, text, frozenset(modules))


def pattern_lines(pattern: Pattern, targets: list[Target]) -> list[Fact]:
    """The lines one pattern owes the targets: each module of its Use a target is or imports and each Search for form a
    target holds, each once, at the first catalogue line that names it, and each form the lister cannot search."""
    lines: list[Fact] = []
    seen: set[tuple[str, str, str]] = set()
    for number, spans in pattern.spans("Use"):
        for span in spans:
            mechanism = lessons_catalogue.MECHANISM.fullmatch(span)
            module = mechanism.group(1) if mechanism else ""
            for target in targets:
                if module in target.modules and ("USE", target.name, module) not in seen:
                    seen.add(("USE", target.name, module))
                    lines.append(("USE", f"{CATALOGUE}:{number}", pattern.name, target.name, module))
    for number, spans in pattern.spans("Search for"):
        for form in spans:
            lines += _form_lines(
                pattern, f"{CATALOGUE}:{number}", form, [t for t in targets if t.text is not None], seen
            )
    return lines


def _form_lines(
    pattern: Pattern, place: str, form: str, targets: list[Target], seen: set[tuple[str, str, str]]
) -> list[Fact]:
    """The lines one Search for form owes the targets whose text was read: where each holds it, or that it cannot be
    searched; nothing for a form an earlier catalogue line of the pattern already searched for that target."""
    lines: list[Fact] = []
    for target in targets:
        if ("SEARCH", target.name, form) in seen:
            continue
        seen.add(("SEARCH", target.name, form))
        try:
            found = form_line(form, target.name, target.text or "")
        except Unsearchable as error:
            return [("UNSEARCHED", place, pattern.name, form, str(error))]
        if found is not None:
            lines.append(("SEARCH", place, pattern.name, f"{target.name}:{found}", form))
    return lines


def listing(root: Path, names: Sequence[str]) -> list[Fact]:
    """The fact of every line the lister prints for the paths `names` in the repository at `root`, in the order it
    prints them, each as its fields, so a caller reads each value without splitting a line;
    CatalogueError when the catalogue cannot be read, and Refused for a path refused."""
    patterns = lessons_catalogue.read_patterns(root)
    lines: list[Fact] = []
    targets = [read_target(root, name, lines) for name in names]
    for pattern in patterns:
        lines += pattern_lines(pattern, targets)
    return lines


def main(argv: Sequence[str] | None = None, root: Path = REPOSITORY) -> int:
    parser = argparse.ArgumentParser(description="List the lessons patterns the files a change touches meet.")
    parser.add_argument("paths", nargs="+", metavar="path", help="a file the change edits or adds, from the top")
    options = parser.parse_args(argv)
    try:
        lines = listing(root, options.paths)
    except (CatalogueError, Refused) as error:
        print(fact_line("FAILED", error))
        return 1
    for fact in lines:
        print(fact_line(*fact))
    return 0


if __name__ == "__main__":
    run_main(main)

"""List the patterns of the lessons catalogue, docs/lessons.md, that the files a change will touch meet, so the
change is planned and written knowing each rule, its helper, and its guard.

    lessons_patterns.py <path> [<path> ...]

Each path is a file the change will edit or add, written from the top of the repository, such as `tools/x.py`. The
catalogue is read through ejryan89-skill-core's lessons_catalogue, the reader validation's ledger check holds it to,
and the patterns are listed in the order it holds them, one line each, with what meets it. A pattern meets a file in
three ways:

- the file is a module its **Use** names, `<module>:<name>`, as it is imported from the repository root or from a
  skill's `scripts` (lessons_catalogue's `mechanism_bases`);
- the file's Python uses such a mechanism: it imports the module, by an `import`, an absolute `from`, or a relative
  `from` resolved against the file's own package, and uses the name the mechanism binds, read through
  ejryan89-skill-core's python_reader;
- one of its **Search for** forms occurs in the file. A form that is an `rg` command, read by Bash's grammar through
  ejryan89-skill-core's bash_syntax, is searched as rg searches: its pattern line by line, or across lines with `-U`,
  inside the paths and `-g` globs it names, then through each `rg` after a `|`, which keeps the lines it matches, or,
  with `-v`, those it does not. Any other form is searched as its text, each `<placeholder>` and `...` in it standing
  for any text on the same line.

A mechanism's use or a form that more than three in a hundred of the repository's tracked files of the file's kind
hold, by its extension, is too common to tell the change anything, such as `console:run_main` or the form `.get(`, and
does not meet the pattern; a file that is the module itself always does. The tracked files are git's `ls-files`, run
through ejryan89-skill-core's git_client only when a file meets a pattern by a use or a form; one that is not there,
not UTF-8, or Python that does not parse holds nothing. A file not there yet is met through its own module alone. The
lister writes nothing, and reads the catalogue, the tracked files, and the paths given, each judged inside the
repository by path_containment's `refusal` before it is opened.

The limits, each a form the session reads itself, which its pattern's prose places:
- a form that is nothing but placeholders is not searched;
- an `rg` pattern is compiled by Python's `re`, which reads the patterns the catalogue writes as rg's regular
  expressions do; one it cannot compile, an rg option the lister does not read, and a command after a `|` that is not
  `rg` are each listed as UNSEARCHED, once for the pattern;
- a relative import that climbs above the top of its file's package names no module, as Python refuses it, and is not
  matched.

`listing` returns each line's fields, never a printed line, for a later fix brief (EJRyan89/coding-agent-skills#2177)
to call for the lessons patterns of the files an issue names.

Every line is one fact, its fields tab-separated:

    PATTERN <catalogue>:<line> <pattern> <evidence> [<evidence> ...]
                                    a pattern, by its heading's line, and each thing that meets it, one field each:
                                    `<path> is <module>`, `<path> uses <mechanism>`, or `<path>:<line> holds <form>`
    UNSEARCHED <catalogue>:<line> <pattern> <form> <why>
                                    a form, at that catalogue line, the lister could not search: search for it by hand
    NEW <path>                      nothing is at the path yet, so only its module is matched
    UNREAD <path> <reason>          a file that is not UTF-8 text, or Python that does not parse: the forms or imports
                                    it holds are not matched
    FAILED <reason>                 the catalogue could not be read, a path is refused or is not a file, a file could
                                    not be examined or read, or git could not list the repository's files

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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_syntax
import file_presence
import lessons_catalogue
import python_reader
from console import run_main
from flat_text import fact_line
from git_client import GitClient, GitError
from lessons_catalogue import CATALOGUE, CatalogueError, Pattern
from path_containment import posix_within, refusal
from text_lines import text_lines

# The checkout this script belongs to, whose catalogue it reads and whose files the paths name.
REPOSITORY = Path(__file__).resolve().parents[4]
# A placeholder in a form, `<name>` or `...`, which stands for any text on the line.
PLACEHOLDER = re.compile(r"<[^<>\s]+>|\.\.\.")
# The share of the repository's tracked files of a kind past which a mechanism's use or a form is too common to tell a
# change anything: more than three files in a hundred.
COMMON_SHARE = 0.03
# The rg options whose value is the next word when the option ends its word.
RG_VALUED = frozenset("gAeBC")
# The rg short options read: line numbers and quiet, which change no match; across lines; inverted.
RG_FLAGS = frozenset("nqUv")
RG_LONG = {"--glob": "g", "--multiline": "U", "--invert-match": "v", "--line-number": "n", "--regexp": "e"}

# One line's fact: its key, such as PATTERN, then its fields, which main prints through fact_line.
type Fact = tuple[str, ...]


class Unsearchable(ValueError):
    """A form the lister cannot search, with the reason."""


class Refused(Exception):
    """A path the lister does not read, with the reason."""


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


def _held(search: RgSearch, name: str) -> bool:
    """Whether the paths and globs of an `rg` hold the file `name`."""
    if search.scopes and not any(scope in {"", "."} or posix_within(name, scope) for scope in search.scopes):
        return False
    base = PurePosixPath(name).name
    return not search.globs or any(
        fnmatch.fnmatchcase(base if "/" not in glob else name, glob) for glob in search.globs
    )


def _line_index(text: str) -> Callable[[int], int]:
    """The line, from 0, an offset of `text` stands on, by the line ends text_lines reads."""
    offsets = list(accumulate((len(line) for line in text_lines(text, keepends=True)), initial=0))
    return lambda offset: bisect_right(offsets, offset) - 1


@dataclass(frozen=True)
class Form:
    """A Search for form as the lister searches it: its `rg` searches, or the pattern its text asks for, or neither,
    for a form that is nothing but placeholders; Unsearchable from `of` for one the lister cannot search."""

    text: str
    searches: tuple[RgSearch, ...] = ()
    pattern: re.Pattern[str] | None = None

    @classmethod
    def of(cls, text: str) -> Form:
        searches = rg_searches(text)
        if searches is not None:
            return cls(text, tuple(searches))
        pieces = PLACEHOLDER.split(text)
        if not "".join(pieces).strip():
            return cls(text)
        # A placeholder stands for text on the line, so no match crosses a line end text_lines reads.
        return cls(text, pattern=re.compile(r"[^\r\n]*?".join(re.escape(piece) for piece in pieces)))

    def holds(self, name: str, text: str) -> bool:
        """Whether the form occurs in the file `name`, whose text is `text`."""
        if self.pattern is not None:
            return self.pattern.search(text) is not None
        return self.first_line(name, text) is not None

    def first_line(self, name: str, text: str) -> int | None:
        """The first line, from 1, of the file `name`, whose text is `text`, at which the form occurs, or None."""
        if self.pattern is not None:
            match = self.pattern.search(text)
            return None if match is None else _line_index(text)(match.start()) + 1
        if not self.searches or not _held(self.searches[0], name):
            return None
        first, filters = self.searches[0], self.searches[1:]
        lines = text_lines(text)
        if first.multiline:
            index = _line_index(text)
            found: Sequence[int] = sorted({index(match.start()) for match in first.pattern.finditer(text)})
        else:
            found = [number for number, line in enumerate(lines) if bool(first.pattern.search(line)) != first.invert]
        for number in found:
            line = lines[number] if number < len(lines) else ""
            if all(bool(search.pattern.search(line)) != search.invert for search in filters):
                return number + 1
        return None


def own_modules(bases: Sequence[Path], root: Path, name: str) -> set[str]:
    """The modules the Python file `name` is, as imported from each folder of `bases` that holds it: a package's
    `__init__.py` is the package. A name that is not an identifier, such as a hyphened folder's, makes a module no
    mechanism names, since a mechanism's module is identifiers alone."""
    if not name.endswith(".py"):
        return set()
    modules: set[str] = set()
    for base in bases:
        folder = "" if base == root else base.relative_to(root).as_posix()
        if folder and not posix_within(name, folder, strict=True):
            continue
        parts = list(PurePosixPath(name[len(folder) + 1 if folder else 0 :]).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.add(".".join(parts))
    return modules


def _relative_base(package: str, level: int) -> str | None:
    """The package a relative import of `level` dots names from `package`, or None when it climbs above its top."""
    parts = package.split(".") if package else []
    if level - 1 >= len(parts):
        return None
    return ".".join(parts[: len(parts) - (level - 1)])


def _import_from(node: ast.ImportFrom, packages: set[str]) -> set[str]:
    """The modules a `from` imports: its module and that module's member it names, which may be a module too, a
    relative one resolved against each of `packages`, the packages the file lies in."""
    if node.level == 0:
        bases = {node.module or ""}
    else:
        bases = {
            ".".join(filter(None, (base, node.module)))
            for package in packages
            if (base := _relative_base(package, node.level)) is not None
        }
    modules: set[str] = set()
    for base in filter(None, bases):
        modules.add(base)
        modules |= {f"{base}.{alias.name}" for alias in node.names}
    return modules


def python_uses(tree: ast.Module, packages: set[str]) -> tuple[set[str], set[str]]:
    """Every module a Python module imports, each `import`'s module with the packages above it and each `from`'s as
    `_import_from` reads it, and every name its code uses: each name it reads or binds, each attribute it reads, and
    each name a `from` imports, so a mechanism `<module>:<name>` is used where `<name>` is. One walk reads both."""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                modules |= {".".join(parts[:end]) for end in range(1, len(parts) + 1)}
        elif isinstance(node, ast.ImportFrom):
            modules |= _import_from(node, packages)
            names |= {alias.name for alias in node.names}
    return modules, names


def packages_of(name: str, modules: set[str]) -> set[str]:
    """The packages a file whose modules are `modules` lies in, against which its relative imports resolve: a
    package's `__init__.py` lies in itself."""
    if PurePosixPath(name).name == "__init__.py":
        return set(modules)
    return {module.rpartition(".")[0] for module in modules}


@dataclass(frozen=True)
class Python:
    """What a Python file's code says about the mechanisms it uses: the modules it imports and the names it uses."""

    imports: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()

    @classmethod
    def read(cls, name: str, text: str, own: set[str]) -> Python:
        """The file's imports and names; PythonRefused when its text does not parse."""
        modules, names = python_uses(python_reader.parse(text), packages_of(name, own))
        return cls(frozenset(modules), frozenset(names))

    def uses(self, module: str, member: str) -> bool:
        """Whether the code imports `module` and uses `member`, the name a mechanism `<module>:<member>` binds."""
        return module in self.imports and member in self.names


@dataclass(frozen=True)
class Target:
    """A path the change touches: its name from the top of the repository, its text, or None when nothing is there
    yet or it cannot be read as text, the modules it is, and what its Python uses."""

    name: str
    text: str | None
    own: frozenset[str]
    python: Python


@dataclass
class Corpus:
    """The repository's tracked files, which say how common a mechanism's use or a form is, read only once asked."""

    root: Path
    bases: Sequence[Path]
    tracked: Callable[[], Sequence[str]]
    _names: Sequence[str] | None = None
    _texts: dict[str, str | None] = field(default_factory=dict)
    _python: dict[str, Python] = field(default_factory=dict)
    _shares: dict[tuple[str, ...], float] = field(default_factory=dict)

    def names(self, suffix: str) -> list[str]:
        if self._names is None:
            self._names = list(self.tracked())
        return [name for name in self._names if PurePosixPath(name).suffix == suffix]

    def text(self, name: str) -> str | None:
        """A tracked file's text; None when it is not there or not UTF-8, which holds nothing; its OSError when it
        cannot be examined or read."""
        if name not in self._texts:
            path = self.root / name
            try:
                self._texts[name] = path.read_text(encoding="utf-8") if file_presence.is_present(path) else None
            except (UnicodeDecodeError, file_presence.NotAFile):
                self._texts[name] = None
        return self._texts[name]

    def python(self, name: str) -> Python:
        """What a tracked Python file's code uses; nothing for one that is not there, not UTF-8, or does not parse."""
        if name not in self._python:
            text = self.text(name)
            try:
                read = Python.read(name, text, own_modules(self.bases, self.root, name)) if text is not None else None
            except python_reader.PythonRefused:
                read = None
            self._python[name] = read or Python()
        return self._python[name]

    def common(self, key: tuple[str, ...], suffix: str, holds: Callable[[str], bool]) -> bool:
        """Whether more than COMMON_SHARE of the tracked files ending in `suffix` hold what `holds` asks about."""
        if key not in self._shares:
            names = self.names(suffix)
            self._shares[key] = sum(map(holds, names)) / len(names) if names else 0.0
        return self._shares[key] > COMMON_SHARE

    def common_use(self, module: str, member: str) -> bool:
        def holds(name: str) -> bool:
            # A file whose text never spells the name, or the module's own name, which every import of it spells,
            # relative or not, cannot use it, so only the others are parsed.
            text = self.text(name)
            spelled = text is not None and member in text and module.rpartition(".")[2] in text
            return spelled and self.python(name).uses(module, member)

        return self.common(("use", module, member), ".py", holds)

    def common_form(self, form: Form, suffix: str) -> bool:
        def holds(name: str) -> bool:
            text = self.text(name)
            return text is not None and form.holds(name, text)

        return self.common(("form", suffix, form.text), suffix, holds)


def read_target(root: Path, bases: Sequence[Path], name: str, facts: list[Fact]) -> Target:
    """The path `name` as the lister reads it, appending a NEW or UNREAD fact it owes to `facts`; Refused for a path
    refused inside the repository or one that is not a file, and the OSError of one that cannot be examined or read."""
    reason = refusal(name, root)
    if reason is not None:
        raise Refused(f"{name} is refused: {reason}")
    name = PurePosixPath(name.replace("\\", "/")).as_posix()
    own = own_modules(bases, root, name)
    path = root / name
    try:
        present = file_presence.is_present(path)
    except file_presence.NotAFile:
        raise Refused(f"{name} is not a file") from None
    if not present:
        facts.append(("NEW", name))
        return Target(name, None, frozenset(own), Python())
    # A file that is not UTF-8 text, such as an image, is named and met through its module alone.
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        facts.append(("UNREAD", name, str(error)))
        return Target(name, None, frozenset(own), Python())
    python = Python()
    if name.endswith(".py"):
        try:
            python = Python.read(name, text, own)
        except python_reader.PythonRefused as error:
            facts.append(("UNREAD", name, str(error)))
    return Target(name, text, frozenset(own), python)


def _use_evidence(pattern: Pattern, targets: list[Target], corpus: Corpus) -> list[str]:
    """What meets a pattern through its Use: each target that is a module it names, and each that imports such a
    module and uses the name the mechanism binds, where no more than COMMON_SHARE of the tracked Python files do."""
    evidence: list[str] = []
    for _, spans in pattern.spans("Use"):
        for span in spans:
            mechanism = lessons_catalogue.MECHANISM.fullmatch(span)
            if mechanism is None:
                continue
            module, member = mechanism.group(1), mechanism.group(2)
            for target in targets:
                if module in target.own:
                    evidence.append(f"{target.name} is {module}")
                elif target.python.uses(module, member) and not corpus.common_use(module, member):
                    evidence.append(f"{target.name} uses {span}")
    return evidence


def _search_evidence(pattern: Pattern, targets: list[Target], corpus: Corpus, facts: list[Fact]) -> list[str]:
    """What meets a pattern through its Search for: each form a target holds that no more than COMMON_SHARE of the
    tracked files of its kind hold; a form the lister cannot search is appended to `facts`, for a pattern any target
    was read for."""
    evidence: list[str] = []
    readable = [target for target in targets if target.text is not None]
    seen: set[str] = set()
    for number, spans in pattern.spans("Search for"):
        for text in spans:
            if text in seen:
                continue
            seen.add(text)
            try:
                form = Form.of(text)
            except Unsearchable as error:
                if readable:
                    facts.append(("UNSEARCHED", f"{CATALOGUE}:{number}", pattern.name, text, str(error)))
                continue
            for target in readable:
                found = form.first_line(target.name, target.text or "")
                if found is not None and not corpus.common_form(form, PurePosixPath(target.name).suffix):
                    evidence.append(f"{target.name}:{found} holds {text}")
    return evidence


def listing(root: Path, names: Sequence[str], tracked: Callable[[], Sequence[str]] | None = None) -> list[Fact]:
    """The fact of every line the lister prints for the paths `names` in the repository at `root`, in the order it
    prints them, each as its fields, so a caller reads each value without splitting a line. `tracked` lists the
    repository's tracked files, git's `ls-files` by default. CatalogueError when the catalogue cannot be read, Refused
    for a path refused, GitError when git cannot list the files, and the OSError of a file that cannot be read."""
    patterns = lessons_catalogue.read_patterns(root)
    bases = lessons_catalogue.mechanism_bases(root)
    facts: list[Fact] = []
    targets = [read_target(root, bases, name, facts) for name in names]
    corpus = Corpus(root, bases, tracked or (lambda: GitClient().names(["ls-files"], directory=root)))
    for pattern in patterns:
        unsearched: list[Fact] = []
        evidence = _use_evidence(pattern, targets, corpus) + _search_evidence(pattern, targets, corpus, unsearched)
        if evidence:
            facts.append(("PATTERN", f"{CATALOGUE}:{pattern.line}", pattern.name, *dict.fromkeys(evidence)))
        facts += dict.fromkeys(unsearched)
    return facts


def main(
    argv: Sequence[str] | None = None, root: Path = REPOSITORY, tracked: Callable[[], Sequence[str]] | None = None
) -> int:
    parser = argparse.ArgumentParser(description="List the lessons patterns the files a change touches meet.")
    parser.add_argument("paths", nargs="+", metavar="path", help="a file the change edits or adds, from the top")
    options = parser.parse_args(argv)
    try:
        facts = listing(root, options.paths, tracked)
    except (CatalogueError, Refused) as error:
        print(fact_line("FAILED", error))
        return 1
    except GitError as error:
        print(fact_line("FAILED", f"git cannot list the repository's files: {error}"))
        return 1
    for fact in facts:
        print(fact_line(*fact))
    return 0


if __name__ == "__main__":
    run_main(main)

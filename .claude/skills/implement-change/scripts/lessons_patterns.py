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
  `from` resolved against the file's own package, and uses the name the mechanism binds, or, for
  `<module>:<class>.<member>`, reads the attribute `member`, as a call on an instance does, read through
  ejryan89-skill-core's python_reader;
- one of its **Search for** forms occurs in the file. A form that is an `rg` command, read by Bash's grammar through
  ejryan89-skill-core's bash_syntax, is searched as rg searches: its pattern line by line, or across lines with `-U`,
  inside the paths and `-g` globs it names, then through each `rg` after a `|`, which keeps the lines it matches, or,
  with `-v`, those it does not. Each `-e` is a pattern, a line matching any of them matching; a glob without a `/`
  matches a file's name, one with a `/` its path, a leading `**/` any folder, and one negated with `!` leaves files
  out. Any other form is searched as its text, each `<placeholder>` and `...` in it standing for any text on the same
  line. A span that names a place in the repository, a tracked file by its path or its name or a folder holding tracked
  files, such as `deployer/` or `update.sh`, is no form: it says where the pattern's forms matter.

A use or a form is common, and too common to tell the change anything, when more than ten of the repository's other
tracked files hold it and more than four in a hundred of them do: among the files of the target's kind, by its
extension, for a use or a form, or among the files of every kind, for a form, so a word is common in a kind with too
few files to say, such as the shell scripts. The files the change touches never count toward their own. A common use
or form meets no pattern, except a form in a file inside a place its pattern's Search for names; a file that is the
module itself always meets it. The tracked files are git's `ls-files`, run through ejryan89-skill-core's git_client
when a file meets a pattern by a use or a form, or a pattern's Search for may name a place; one that is not there,
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
# The share of the repository's other tracked files of a kind past which a mechanism's use or a form is too common to
# tell a change anything: more than four files in a hundred.
COMMON_SHARE = 0.04
# The number of other tracked files of a kind a use or a form must pass to be common too, so a kind with few files, such
# as the shell scripts, never cuts a form only a handful hold.
COMMON_FLOOR = 10
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
    # Each -e names a pattern, and a line matching any of them matches; without one, the first word is the pattern.
    patterns, scopes = (options["e"], positional) if options.get("e") else (positional[:1], positional[1:])
    if not patterns:
        return None
    try:
        pattern = re.compile("|".join(f"(?:{each})" for each in patterns), re.MULTILINE)
    except re.error as error:
        raise Unsearchable(f"Python's re cannot compile the pattern: {error}") from None
    return RgSearch(
        pattern,
        "U" in options,
        "v" in options,
        tuple(scope.rstrip("/") for scope in scopes),
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


def _glob_matches(glob: str, name: str) -> bool:
    """Whether an rg glob matches the file `name`: one with no `/` matches its base name, one with a `/` the whole
    path, and a leading `**/` any folder, the top included."""
    if "/" not in glob:
        return fnmatch.fnmatchcase(PurePosixPath(name).name, glob)
    return fnmatch.fnmatchcase(name, glob) or (glob.startswith("**/") and _glob_matches(glob[3:], name))


def _held(search: RgSearch, name: str) -> bool:
    """Whether the paths and globs of an `rg` hold the file `name`: inside one of its paths, matching one of its
    globs when it has any that are not negated, and none of those negated with `!`."""
    if search.scopes and not any(scope in {"", "."} or posix_within(name, scope) for scope in search.scopes):
        return False
    kept = [glob for glob in search.globs if not glob.startswith("!")]
    left_out = [glob[1:] for glob in search.globs if glob.startswith("!")]
    if kept and not any(_glob_matches(glob, name) for glob in kept):
        return False
    return not any(_glob_matches(glob, name) for glob in left_out)


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
            # A match that starts past the last line break, as an empty match at the text's end does, is on no line.
            starts = {index(match.start()) for match in first.pattern.finditer(text)}
            found: Sequence[int] = sorted(number for number in starts if number < len(lines))
        else:
            found = [number for number, line in enumerate(lines) if bool(first.pattern.search(line)) != first.invert]
        for number in found:
            if all(bool(search.pattern.search(lines[number])) != search.invert for search in filters):
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


def python_uses(tree: ast.Module, packages: set[str]) -> tuple[set[str], set[str], set[str]]:
    """Every module a Python module imports, each `import`'s module with the packages above it and each `from`'s as
    `_import_from` reads it; every name its code reads or binds and each name a `from` imports; and every attribute it
    reads. One walk reads all three."""
    modules: set[str] = set()
    names: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                modules |= {".".join(parts[:end]) for end in range(1, len(parts) + 1)}
        elif isinstance(node, ast.ImportFrom):
            modules |= _import_from(node, packages)
            names |= {alias.name for alias in node.names}
    return modules, names, attributes


def packages_of(name: str, modules: set[str]) -> set[str]:
    """The packages a file whose modules are `modules` lies in, against which its relative imports resolve: a
    package's `__init__.py` lies in itself."""
    if PurePosixPath(name).name == "__init__.py":
        return set(modules)
    return {module.rpartition(".")[0] for module in modules}


@dataclass(frozen=True)
class Python:
    """What a Python file's code says about the mechanisms it uses: the modules it imports, the names it uses, and the
    attributes it reads."""

    imports: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    attributes: frozenset[str] = frozenset()

    @classmethod
    def read(cls, name: str, text: str, own: set[str]) -> Python:
        """The file's imports, names, and attributes; PythonRefused when its text does not parse."""
        modules, names, attributes = python_uses(python_reader.parse(text), packages_of(name, own))
        return cls(frozenset(modules), frozenset(names), frozenset(attributes))

    def uses(self, module: str, name: str, member: str | None) -> bool:
        """Whether the code imports `module` and uses a mechanism of it: for `<module>:<class>.<member>`, reads the
        attribute `member`, as a call on an instance does; for `<module>:<name>`, uses `name` as a name or as the
        module's attribute."""
        if module not in self.imports:
            return False
        return member in self.attributes if member else name in self.names or name in self.attributes


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
    # The files the change touches, which never count toward how common their own uses and forms are.
    targets: frozenset[str] = frozenset()
    _names: Sequence[str] | None = None
    _places: frozenset[str] | None = None
    _texts: dict[str, str | None] = field(default_factory=dict)
    _python: dict[str, Python] = field(default_factory=dict)
    _shares: dict[tuple[str, ...], float] = field(default_factory=dict)

    def every_name(self) -> Sequence[str]:
        if self._names is None:
            self._names = list(self.tracked())
        return self._names

    def names(self, suffix: str | None) -> list[str]:
        """The tracked files ending in `suffix`, or every tracked file for None, the change's own left out."""
        return [
            name
            for name in self.every_name()
            if (suffix is None or PurePosixPath(name).suffix == suffix) and name not in self.targets
        ]

    def is_place(self, span: str) -> bool:
        """Whether a Search for span names a place in the repository rather than a form: a tracked file by its path or
        its name, or a folder that holds tracked files."""
        if self._places is None:
            names = self.every_name()
            # Each folder a tracked path lies in, by the `/`-separated parts git gives it.
            folders = {
                "/".join(parts[:end]) for parts in (name.split("/") for name in names) for end in range(1, len(parts))
            }
            self._places = frozenset(names) | {PurePosixPath(name).name for name in names} | folders
        return span.rstrip("/") in self._places

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

    def common(self, key: tuple[str, ...], suffix: str | None, holds: Callable[[str], bool]) -> bool:
        """Whether what `holds` asks about is common among the other tracked files ending in `suffix`, or among all of
        them for None: more than COMMON_FLOOR of them hold it, and more than COMMON_SHARE of them."""
        if key not in self._shares:
            names = self.names(suffix)
            count = sum(map(holds, names))
            self._shares[key] = count / len(names) if count > COMMON_FLOOR else 0.0
        return self._shares[key] > COMMON_SHARE

    def common_use(self, module: str, bound: str, member: str | None) -> bool:
        def holds(name: str) -> bool:
            # A file whose text never spells the name it would use, or the module's own name, which every import of
            # it spells, relative or not, cannot use it, so only the others are parsed.
            text = self.text(name)
            spelled = text is not None and (member or bound) in text and module.rpartition(".")[2] in text
            return spelled and self.python(name).uses(module, bound, member)

        return self.common(("use", module, bound, member or ""), ".py", holds)

    def common_form(self, form: Form, suffix: str) -> bool:
        def holds(name: str) -> bool:
            text = self.text(name)
            return text is not None and form.holds(name, text)

        # A form common among the files of its kind, or among the files of every kind, as a word is in a kind with
        # too few files to say, tells the change nothing.
        within_kind = self.common(("form", suffix, form.text), suffix, holds)
        return within_kind or self.common(("form", "", form.text), None, holds)


def _in_place(name: str, place: str) -> bool:
    """Whether the file `name` is the place a Search for span names: the file by its path or its name, or a file in
    the folder."""
    return name == place or PurePosixPath(name).name == place or posix_within(name, place, strict=True)


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
    module and uses the name the mechanism binds, its member's for `<module>:<class>.<member>`, where using it is not
    common among the other tracked Python files."""
    evidence: list[str] = []
    for _, spans in pattern.spans("Use"):
        for span in spans:
            mechanism = lessons_catalogue.MECHANISM.fullmatch(span)
            if mechanism is None:
                continue
            module, bound, member = mechanism.groups()
            for target in targets:
                if module in target.own:
                    evidence.append(f"{target.name} is {module}")
                elif target.python.uses(module, bound, member) and not corpus.common_use(module, bound, member):
                    evidence.append(f"{target.name} uses {span}")
    return evidence


def _search_evidence(pattern: Pattern, targets: list[Target], corpus: Corpus, facts: list[Fact]) -> list[str]:
    """What meets a pattern through its Search for: each form a target holds, where the target lies in a place the
    Search for names or the form is not common among the other tracked files of its kind; a span that names a place
    is no form. A form the lister cannot search is appended to `facts`, for a pattern any target was read for."""
    evidence: list[str] = []
    readable = [(target, target.text) for target in targets if target.text is not None]
    spans = [span for _, line_spans in pattern.spans("Search for") for span in line_spans]
    places = [span.rstrip("/") for span in spans if readable and corpus.is_place(span)]
    seen = set(places) | {f"{place}/" for place in places}
    for number, line_spans in pattern.spans("Search for"):
        for text in line_spans:
            if text in seen:
                continue
            seen.add(text)
            try:
                form = Form.of(text)
            except Unsearchable as error:
                if readable:
                    facts.append(("UNSEARCHED", f"{CATALOGUE}:{number}", pattern.name, text, str(error)))
                continue
            for target, text_read in readable:
                found = form.first_line(target.name, text_read)
                if found is None:
                    continue
                placed = any(_in_place(target.name, place) for place in places)
                if placed or not corpus.common_form(form, PurePosixPath(target.name).suffix):
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
    lister = tracked or (lambda: GitClient().names(["ls-files"], directory=root))
    corpus = Corpus(root, bases, lister, frozenset(target.name for target in targets))
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

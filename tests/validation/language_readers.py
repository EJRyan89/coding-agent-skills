r"""The language reader policy: a regular expression that spells a language's structure stands in the module that owns
the language, and every other reader of that language goes through it.

Bash has one grammar, ejryan89-skill-core's `bash_syntax`; Markdown's blocks, headings, code spans, and a console
fence's prompts have one walk, `markdown_sections`; and the characters of a path written in prose have one class,
`prose_paths`. A reader that spells one of their structures with a pattern of its own reads the language its own way,
and misses what the owner reads, as each reader since moved onto its owner did. The policy reads the
pattern text of each `re` call, `re.compile` and the match family, through `ast`, its constant text and an f-string's
constant parts, resolved through the module's imports, and refuses each family below outside its owner. It also refuses
the one non-pattern form of the family, a brace-depth count, `x.count("{") - y.count("}")`. A reader that must stay
states why beside the code, as a module-level LANGUAGE_READER_ALLOWED = {scope: reason}, keyed by the function or the
assignment that holds it, and an entry that sanctions nothing is reported.

Its stated limits, each a passing row of the fixture suite. The repository writes two of them, and neither reads a
language's structure for a reader: audit-skill's cue fragments, joined into its cue patterns through names, each
match a candidate its agent judges, and its literal model and effort, a word quoted by optional backticks.
- It reads only the text pattern a call is given first, as a literal or an f-string's constant parts. Not read are a
  pattern a module holds as a plain string and joins into a pattern later through a name, as an f-string's `{NAME}`;
  one given as bytes or by `pattern=`; one built with `+`, `%`, or `.format`; one reached through `from re import *`;
  a glob given to `fnmatch`; a `re.VERBOSE` pattern whose blanks split a family's spelling; and a pattern of the
  owner's used line by line, as `ATX_HEADING.match(line)` on a line no block walk read.
- Each family is read in the spellings the repository writes, the forms the fixture suite lists. Not read are: a
  function header's `function` followed by a literal space; heading marks as `#+` inside a group, as `#{2,}`, or
  followed by `\s`; a comment test with anything after its `#`, or indented by ` *`; a prompt after `\s+`, `(?m)^`,
  or ` *`; a prose path with no lookbehind, folders counted `{1,}`, or a backslash in its separator; a code span whose
  body starts with literal text, as `` `--[\w-]+ ``, or a literal word quoted by optional backticks, as
  `` `?haiku`? ``, which reads the word, not a span; a span whose class comes behind two lookarounds, nested groups,
  or a group and then a lookahead, as `` `(?!-)(?!\s)[^`]+` ``, `` `((?:[^`]+))` ``, or `` `(?:(?!`).)+` ``, behind a
  lookaround holding a class or longer than 64 characters, or in a conditional group; a span opened by a class or an
  escape that holds the backtick, as `` [`]([^`]+)[`] `` or `\x60`; a header's bare `\(\)` before `\s+`, ` *`, a
  literal space, or no `{`, and `\( *\)`; and a brace count written other than `a.count("{") - b.count("}")`.
  A backtick before an unescaped `.`, as `` `(.+?)` ``, which no code here writes, is read as a span.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from duplication import DUPLICATION_ROOTS, duplication_omissions
from python_checks import SKILL_CORE_SCRIPTS, sanctioned_problems
from validation_support import (
    REPOSITORY_ROOT,
    CallableResolver,
    is_test_script,
    python_trees,
    repository_files,
    scope_problems,
    scoped_nodes,
    under,
)

ALLOWANCE = "LANGUAGE_READER_ALLOWED"
BASH_OWNER = f"{SKILL_CORE_SCRIPTS}/bash_syntax.py"
MARKDOWN_OWNER = f"{SKILL_CORE_SCRIPTS}/markdown_sections.py"
PROSE_PATH_OWNER = f"{SKILL_CORE_SCRIPTS}/prose_paths.py"
# The trees of the repository's own Python the policy reads are the duplication policy's, DUPLICATION_ROOTS: their
# suites state what they test as literals of their own, and fixtures are other trees.
FIXTURES = "tests/fixtures"
# The `re` functions given a pattern as their first argument.
PATTERN_CALLS = frozenset(
    f"re.{name}" for name in ("compile", "match", "fullmatch", "search", "findall", "finditer", "sub", "subn", "split")
)
# What stands for an f-string's replacement field in the text a family is searched in.
FIELD = "{}"


@dataclass(frozen=True)
class Family:
    """A structure of a language a pattern may spell, the module that owns its reading, and how its spelling reads in
    a pattern's text."""

    name: str
    owner: str
    spelling: re.Pattern[str]


FAMILIES = (
    # A Bash function's header: a name's empty parentheses with blanks allowed inside, or bare ones before the body's
    # `{`, or the `function` keyword and its blanks.
    Family(
        "a Bash function header",
        BASH_OWNER,
        re.compile(r"\\\((?:\\s|\[ \\t\])\*\\\)|\\\(\\\)(?:\\s\*|\[ \\t\]\*)?\\\{|\bfunction(?:\\s|\[ \\t\])\+"),
    ),
    # A whole pattern that asks whether a line is a `#` comment: an optional anchor, blanks, and `#`, nothing after.
    Family("a `#` comment test", BASH_OWNER, re.compile(r"\A(?:\^|\\A)?(?:\\s\*|\[ \\t\]\*|\[\\t \]\*)?#\Z")),
    # An ATX heading's marks: `#{1,6}`, or the pattern's first `#`s, after any anchor and indentation, then a blank.
    Family(
        "an ATX heading",
        MARKDOWN_OWNER,
        re.compile(r"#\{1,6\}|\A(?:\^|\\A)?(?: \{0,3\})?#+(?:\{\d(?:,\d)?\})?(?: |\[ \\t\]|\[\\t \])"),
    ),
    # A code span read by single backticks: a backtick, then, after at most one whole lookaround or inline-flag group
    # and at most one group's opening, plain, named, non-capturing, atomic, or flagged, a class of what the span holds:
    # a bracketed one, positive or negated, an escape's, or `.`. A backtick that opens a class, as `[^`\s]`, is the
    # class's, not a span's. A lookaround's text is read up to 64 characters, and every other run stops at a
    # backtick, so each backtick starts a bounded search and the whole is linear in the pattern's length.
    Family(
        "a single-backtick code span",
        MARKDOWN_OWNER,
        re.compile(
            r"(?<!\[)(?<!\[\^)`(?:\(\?(?:<?[!=][^)\[]{0,64}|[aiLmsux-]+)\))?(?:\((?:\?(?:P<\w+>|[:>]|[aiLmsux-]+:))?)?"
            r"(?:\[|\\[wWsSdD]|\.)"
        ),
    ),
    # A console fence's prompt: at the line's start, after blanks, `$ ` or `> `.
    Family(
        "a fence prompt",
        MARKDOWN_OWNER,
        re.compile(r"\A(?:\^|\\A)(?:\[ \\t\]\*|\\s\*)?(?:\\\$ |> |\[\$>\] |\(\?:\[\$>\] \)\??)"),
    ),
    # A path written bare in prose: a lookbehind that keeps it from starting inside another, then folders, each a run
    # of a class and a `/`, at least one.
    Family("a prose path", PROSE_PATH_OWNER, re.compile(r"\(\?<!\[[^\]]*\]\)\(\?:\[[^\]]*\][+*]/\)\+")),
)
BRACE_COUNT = Family("a brace-depth count", BASH_OWNER, re.compile(r"\Z\A"))


def pattern_text(node: ast.expr) -> str | None:
    """The text of a pattern argument the policy reads: a string literal's, or an f-string's constant parts, each
    replacement field written FIELD; None for any other expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else FIELD
            for value in node.values
        )
    return None


def counts_brace(node: ast.AST, brace: str) -> bool:
    """Whether a node is a call of `.count` given the one literal `brace`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "count"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == brace
    )


def spelled_families(tree: ast.Module) -> Iterator[tuple[int, str, Family]]:
    """Each family a module's patterns spell, and each brace-depth count it makes, as the line, the scope, and the
    family."""
    resolver = CallableResolver(tree)
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            if counts_brace(node.left, "{") and counts_brace(node.right, "}"):
                yield node.lineno, scope, BRACE_COUNT
            continue
        if not isinstance(node, ast.Call) or not node.args or resolver.name(node.func) not in PATTERN_CALLS:
            continue
        text = pattern_text(node.args[0])
        if text is None:
            continue
        for family in FAMILIES:
            if family.spelling.search(text):
                yield node.lineno, scope, family


def reader_files(root: Path, files: list[Path]) -> list[Path]:
    """The Python the policy reads: every module under DUPLICATION_ROOTS but a suite and a fixture."""
    return sorted(
        path
        for path in files
        if path.suffix == ".py"
        and under(path.relative_to(root).as_posix(), DUPLICATION_ROOTS)
        and not under(path.relative_to(root).as_posix(), (FIXTURES,))
        and not is_test_script(path)
    )


def language_reader_problems(root: Path, files: list[Path]) -> list[str]:
    """Each pattern, or brace-depth count, that spells a language's structure outside the module that owns it, by its
    line, unless its scope's LANGUAGE_READER_ALLOWED entry states why; each entry that sanctions nothing; and that no
    file was read when none was, since a policy that read nothing found nothing."""
    read = reader_files(root, files)
    if not read:
        return ["no Python file was read, so the language reader policy could not judge anything"]
    problems: list[str] = []
    for _, name, tree in python_trees(read, root, problems):
        found = [
            (
                line,
                scope,
                f"spells {family.name} outside {family.owner}, the module that owns its reading; read it through "
                f"that module, or state why in {ALLOWANCE}",
            )
            for line, scope, family in spelled_families(tree)
            if name != family.owner
        ]
        problems += sanctioned_problems(name, tree, ALLOWANCE, found)
    return problems


def unread_python(root: Path, files: list[Path]) -> list[str]:
    """Each module the policy's rule speaks of that it does not read: the repository's Python outside its roots, but
    the parts the duplication policy leaves out for the reasons it gives, as the two read the same trees."""
    names = [path.relative_to(root).as_posix() for path in files if path.suffix == ".py" and not is_test_script(path)]
    return scope_problems("the language reader policy", names, DUPLICATION_ROOTS, duplication_omissions())


class LanguageReaderPolicies(unittest.TestCase):
    def test_no_pattern_spells_a_language_s_structure_outside_its_owner(self) -> None:
        self.assertEqual([], language_reader_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_the_language_reader_policy_reads_every_module_its_rule_speaks_of(self) -> None:
        self.assertEqual([], unread_python(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

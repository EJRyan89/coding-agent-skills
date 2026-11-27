"""The language reader policy: a regular expression that spells a language's structure stands in the module that owns
the language, and every other reader of that language goes through it.

Bash has one grammar, ejryan89-skill-core's `bash_syntax`; Markdown's blocks, headings, code spans, and a console
fence's prompts have one walk, `markdown_sections`; and the characters of a path written in prose have one class,
`prose_paths`. A reader that spells one of their structures with a pattern of its own reads the language its own way,
and misses what the owner reads, as each reader moved onto the owner by #3530 and #3533 did. The policy reads the
pattern text of each `re` call, `re.compile` and the match family, through `ast`, its constant text and an f-string's
constant parts, resolved through the module's imports, and refuses each family below outside its owner. It also refuses
the one non-pattern form of the family, a brace-depth count, `x.count("{") - y.count("}")`. A reader that must stay
states why beside the code, as a module-level LANGUAGE_READER_ALLOWED = {scope: reason}, keyed by the function or the
assignment that holds it, and an entry that sanctions nothing is reported.

Its stated limits:
- It reads only the pattern a call is given. A pattern a module holds as a plain string, joined into a pattern later
  through a name, as an f-string's `{NAME}`, and a pattern of the owner's used line by line, as
  `ATX_HEADING.match(line)` on a line no block walk read, are not read.
- Each family is spelled as the repository writes it, by the forms the fixture suite lists; another spelling of the
  same structure, as a heading's marks written `#####?` or a span's body written without a negated class, is not read.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from python_checks import sanctioned_problems
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
SKILL_CORE = "skills/ejryan89-skill-core/scripts"
BASH_OWNER = f"{SKILL_CORE}/bash_syntax.py"
MARKDOWN_OWNER = f"{SKILL_CORE}/markdown_sections.py"
PROSE_PATH_OWNER = f"{SKILL_CORE}/prose_paths.py"
# The trees of the repository's own Python the policy reads, as the duplication policy reads them; their suites state
# what they test as literals of their own, and fixtures are other trees.
READER_ROOTS = ("skills", "deployer", "tools", ".claude/skills", "tests/validation")
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
    # A Bash function's header: a name's empty parentheses, blanks allowed inside, or the `function` keyword.
    Family(
        "a Bash function header",
        BASH_OWNER,
        re.compile(r"\\\((?:\\s|\[ \\t\])\*\\\)|\bfunction(?:\\s|\[ \\t\])\+"),
    ),
    # A whole pattern that asks whether a line is a `#` comment: an optional anchor, blanks, and `#`, nothing after.
    Family("a `#` comment test", BASH_OWNER, re.compile(r"\A(?:\^|\\A)?(?:\\s\*|\[ \\t\]\*|\[\\t \]\*)?#\Z")),
    # An ATX heading's marks: `#{1,6}`, or the pattern's first `#`s, after any anchor and indentation, then a blank.
    Family(
        "an ATX heading",
        MARKDOWN_OWNER,
        re.compile(r"#\{1,6\}|\A(?:\^|\\A)?(?: \{0,3\})?#+(?:\{\d(?:,\d)?\})?(?: |\[ \\t\]|\[\\t \])"),
    ),
    # A code span read by single backticks: a backtick, then, inside any group or after any lookahead, a class of
    # everything but a backtick.
    Family(
        "a single-backtick code span",
        MARKDOWN_OWNER,
        re.compile(r"`(?:\((?:\?[:!=](?:[^)\[]*\))?)?)*\[\^[^\]]*`"),
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
    """The Python the policy reads: every module under READER_ROOTS but a suite and a fixture."""
    return sorted(
        path
        for path in files
        if path.suffix == ".py"
        and under(path.relative_to(root).as_posix(), READER_ROOTS)
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
    the suites, the fixtures, and the root modules, which state what they test or share nothing a tree imports."""
    names = [path.relative_to(root).as_posix() for path in files if path.suffix == ".py" and not is_test_script(path)]
    reason = "outside the trees the rule names: the suites state what they test as literals of their own"
    omitted = {"tests": reason, "deploy.py": reason, "conftest.py": reason}
    return scope_problems("the language reader policy", names, READER_ROOTS, omitted)


class LanguageReaderPolicies(unittest.TestCase):
    def test_no_pattern_spells_a_language_s_structure_outside_its_owner(self) -> None:
        self.assertEqual([], language_reader_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_the_language_reader_policy_reads_every_module_its_rule_speaks_of(self) -> None:
        self.assertEqual([], unread_python(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

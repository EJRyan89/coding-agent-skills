"""Documents that point elsewhere keep pointing at something: relative Markdown links and their fragments, and the
tests the code-review threat model cites.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from validation_support import REPOSITORY_ROOT, _markdown_section, fence_holders, repository_files

MARKDOWN_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
MARKDOWN_CODE_SPAN = re.compile(r"(`+).+?\1")
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
            text = MARKDOWN_CODE_SPAN.sub("", line)
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


THREAT_MODEL_DOC = "docs/code-review-operations-contract.md"
THREAT_MODEL_HEADING = "## Threat model"
THREAT_MODEL_SUITES = "skills/code-review-core/scripts"
# The suite that holds the threat model one row at a time: every row names at least one of its tests.
THREAT_MODEL_ADVERSARIAL_SUITE = "test_adversarial_inputs.py"
CITED_TEST = re.compile(r"`(test_[\w-]+\.py)::(test_\w+)`")


def threat_model_test_problems(root: Path) -> list[str]:
    """Report each row of the code-review threat model that names no test in test_adversarial_inputs.py, and each
    `<suite>::<test>` it names that is not a test function in that suite under skills/code-review-core/scripts/."""
    section = _markdown_section((root / THREAT_MODEL_DOC).read_text(encoding="utf-8"), THREAT_MODEL_HEADING)
    rows = [line for line in (section or "").split("\n") if line.startswith("|")][2:]
    if not rows:
        return [f"{THREAT_MODEL_DOC} has no table under {THREAT_MODEL_HEADING!r}"]
    defined: dict[str, set[str] | None] = {}
    problems: list[str] = []
    for row in rows:
        cited = CITED_TEST.findall(row)
        if all(suite != THREAT_MODEL_ADVERSARIAL_SUITE for suite, _ in cited):
            problems.append(
                f"{THREAT_MODEL_DOC}: the threat-model row {row.split('|')[1].strip()!r} names no test in "
                f"{THREAT_MODEL_ADVERSARIAL_SUITE}"
            )
        for suite, test in cited:
            if suite not in defined:
                path = root / THREAT_MODEL_SUITES / suite
                defined[suite] = (
                    {
                        node.name
                        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    if path.is_file()
                    else None
                )
            names = defined[suite]
            if names is None:
                problems.append(
                    f"{THREAT_MODEL_DOC} names {suite}::{test}, but {THREAT_MODEL_SUITES}/{suite} does not exist"
                )
            elif test not in names:
                problems.append(f"{THREAT_MODEL_DOC} names {suite}::{test}, which {suite} does not define")
    return problems


class MarkdownLinksPolicies(unittest.TestCase):
    def test_repository_links_resolve(self) -> None:
        self.assertEqual([], markdown_link_problems(REPOSITORY_ROOT))

    def test_every_test_the_threat_model_names_exists(self) -> None:
        self.assertEqual([], threat_model_test_problems(REPOSITORY_ROOT))

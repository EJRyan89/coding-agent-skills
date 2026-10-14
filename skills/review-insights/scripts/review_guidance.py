"""What the synthesis reads of each repository's reviewer guidance: the files each repository-scoped reviewer was built
from, the text of its prose files as that reviewer was given it, and the candidate overlaps a script can find in that
text before any model reads it.

A reviewer's files are the repository-relative paths in `review.adapter.source_hashes` of the latest record it wrote,
anywhere in the archive; a profile the suite ships is no file of the repository and is left out. A prose file's text
is read from the repository's configured checkout only when its SHA-256 matches the hash that record pinned: the file
in the working tree, or else the file at the record's `source_commit`. A file whose text does not match, or that is
too large or past the input's total budget, is listed by path alone with the reason. A script, manifest, or other code
file is listed by path, so a recommendation can name it, and its text is never read.

The overlap candidates come from the text read, every verified file, including one listed without its text for the
budget: each rule (a list item's or paragraph's sentence) is compared with the rules of the repository's other files
by the words they share, leaving out the words that set a rule's force (must, should, may, never, a severity name)
and its numbers. Two rules that share most of their words and differ in those are a candidate `conflict`; two that
share nearly all of them and agree in those are a candidate `duplicate`. A reference to a Markdown file a reviewer
was not given is an `unsupplied` candidate. The synthesis judges each candidate; the script only finds them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from flat_text import flat_text
from git_client import GitClient, GitError
from review_config import safe_repository_path
from review_records import is_suite_profile

PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc"})
# The text the input carries: each file at most FILE_CHARS, all of them at most TOTAL_CHARS, near 12k tokens.
FILE_CHARS = 16_000
TOTAL_CHARS = 48_000
# A file larger than this is never read, so a stray binary or generated file cannot stall the report.
READ_BYTES = 1024 * 1024
RULE_TEXT = 300
MIN_WORDS = 4
CONFLICT_SIMILARITY = 0.6
DUPLICATE_SIMILARITY = 0.75
STOPWORDS = frozenset(
    {
        *("a", "an", "and", "are", "as", "at", "be", "been", "by", "each", "for", "from", "has", "have", "if", "in"),
        *("into", "is", "it", "its", "of", "on", "or", "so", "than", "that", "the", "their", "them", "then", "there"),
        *("these", "this", "those", "to", "was", "were", "when", "where", "which", "while", "who", "will", "with"),
    }
)
# The words that set a rule's force or polarity, each with the force it states: two rules that differ only in these
# disagree about how hard the rule applies.
FORCE_WORDS = {
    "must": "must",
    "shall": "must",
    "required": "must",
    "requires": "must",
    "always": "always",
    "should": "should",
    "recommended": "should",
    "prefer": "should",
    "may": "may",
    "optional": "may",
    "can": "may",
    "never": "never",
    "not": "not",
    "no": "not",
    "avoid": "not",
    "must_fix": "must_fix",
    "should_fix": "should_fix",
    "suggestion": "suggestion",
    "nit": "suggestion",
    "blocking": "blocking",
    "critical": "critical",
}
LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+(.*)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*_(\"'])")
WORD = re.compile(r"[a-z0-9_]+")
LINK = re.compile(r"\]\(\s*<?([^)\s>#]+\.md)(?:#[^)\s]*)?>?\s*\)")
CODE_REFERENCE = re.compile(r"`([A-Za-z0-9_./-]+\.md)`")

PinnedReader = Callable[[Path, str | None, str, str], bytes | None]


@dataclass
class Source:
    """The latest record a repository-scoped reviewer wrote: when, at which reviewer commit, and its files' hashes."""

    stamp: str
    commit: str | None
    hashes: dict[str, str]


@dataclass
class Rule:
    path: str
    line: int
    text: str
    words: frozenset[str]
    force: frozenset[str]
    numbers: frozenset[str]


@dataclass
class Guidance:
    """Each repository's guidance files by path, the input's line for each file, and the candidates found in them."""

    files: dict[str, list[str]]
    lines: list[dict[str, Any]] = field(default_factory=list)
    overlaps: list[dict[str, Any]] = field(default_factory=list)
    unsupplied: list[dict[str, Any]] = field(default_factory=list)


def reviewer_sources(repositories: list[str], records: dict[Path, dict[str, Any]]) -> dict[str, dict[str, Source]]:
    """Each repository's repository-scoped reviewers, each with the latest record it wrote, anywhere in the archive.
    `records` holds every validated record of the repositories by its path."""
    latest: dict[str, dict[str, Source]] = {repository.lower(): {} for repository in repositories}
    for path in sorted(records):
        record = records[path]
        review, by_name = record["review"], latest.get(record["repository"].lower())
        adapter = review["adapter"]
        if by_name is None or adapter["scope"] != "repository" or not adapter["source_hashes"]:
            continue
        stamp = review["reviewed_at"]
        if adapter["name"] not in by_name or by_name[adapter["name"]].stamp < stamp:
            hashes = {key: value for key, value in adapter["source_hashes"].items() if not is_suite_profile(key)}
            by_name[adapter["name"]] = Source(stamp, adapter.get("source_commit"), hashes)
    return latest


def guidance_files(repositories: list[str], records: dict[Path, dict[str, Any]]) -> dict[str, list[str]]:
    """Each repository's guidance files: the source files of the latest record each repository-scoped reviewer
    wrote. A profile the suite ships is recorded beside them under its `suite:<name>` reference, but it is no file of
    the repository, which cannot change it, so it is left out."""
    return {
        repository: sorted({path for source in by_name.values() for path in source.hashes})
        for repository, by_name in reviewer_sources(repositories, records).items()
    }


def is_prose(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PROSE_SUFFIXES


def read_pinned(checkout: Path, commit: str | None, path: str, digest: str) -> bytes | None:
    """The file's bytes as the reviewer was given them: the working tree's copy, or else the copy at `commit`, the
    first whose SHA-256 is `digest`; None when neither is, or the path leaves the checkout."""
    if not safe_repository_path(path):
        return None
    root = checkout.resolve()
    target = root.joinpath(*PurePosixPath(path).parts).resolve()
    if target.is_relative_to(root) and target.is_file() and target.stat().st_size <= READ_BYTES:
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() == digest:
            return content
    if commit is None:
        return None
    try:
        result = GitClient().run(["show", f"{commit}:{path}"], directory=checkout)
    except GitError:
        return None
    content = result.output_bytes()
    if result.returncode != 0 or len(content) > READ_BYTES or hashlib.sha256(content).hexdigest() != digest:
        return None
    return content


def _file_sources(by_name: dict[str, Source]) -> dict[str, tuple[Source, list[str]]]:
    """Each file of a repository with the latest reviewer record that names it, and every reviewer that names it."""
    files: dict[str, tuple[Source, list[str]]] = {}
    for name in sorted(by_name):
        source = by_name[name]
        for path in source.hashes:
            known = files.get(path)
            newest = source if known is None or known[0].stamp < source.stamp else known[0]
            files[path] = (newest, [*(known[1] if known else []), name])
    return files


def read_guidance(
    repositories: list[str],
    records: dict[Path, dict[str, Any]],
    checkouts: Mapping[str, Path | None],
    reader: PinnedReader = read_pinned,
) -> Guidance:
    """The guidance the synthesis reads: each repository's files, a line for each with its text when it is prose,
    verified, and within the budget, and the overlap and unsupplied-reference candidates in the text read."""
    sources = reviewer_sources(repositories, records)
    guidance = Guidance(guidance_files(repositories, records))
    budget = TOTAL_CHARS
    for repository in sorted(sources):
        checkout = checkouts.get(repository)
        texts: dict[str, str] = {}
        for path, (source, reviewers) in sorted(_file_sources(sources[repository]).items()):
            line: dict[str, Any] = {
                "kind": "guidance-file",
                "repository": repository,
                "path": path,
                "role": "prose" if is_prose(path) else "code",
                "reviewers": reviewers,
                "status": "listed",
                "content": None,
            }
            if is_prose(path):
                text = _verified_text(checkout, source, path, reader)
                if text is not None:
                    texts[path] = text
                line["status"] = _status(text, budget)
                if line["status"] == "included" and text is not None:
                    line["content"] = text
                    budget -= len(text)
            guidance.lines.append(line)
        guidance.overlaps += overlaps(repository, texts, sources[repository])
        guidance.unsupplied += unsupplied(repository, texts, sources[repository])
    guidance.overlaps.sort(key=_overlap_rank)
    return guidance


def _verified_text(checkout: Path | None, source: Source, path: str, reader: PinnedReader) -> str | None:
    if checkout is None:
        return None
    content = reader(checkout, source.commit, path, source.hashes[path])
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _status(text: str | None, budget: int) -> str:
    """`unavailable` with no verified text, `too-large` past the file limit, `over-budget` past what the input has
    left, otherwise `included`."""
    if text is None:
        return "unavailable"
    if len(text) > FILE_CHARS:
        return "too-large"
    return "over-budget" if len(text) > budget else "included"


def _statements(text: str) -> list[tuple[int, str]]:
    """Each list item and paragraph of Markdown, with the line it starts on; headings, tables, and code are left
    out."""
    found: list[tuple[int, str]] = []
    continued = fenced = False
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        item = LIST_ITEM.fullmatch(raw)
        if fenced or not stripped or stripped.startswith(("```", "~~~", "#", "|")):
            continued = False
        elif item is not None or not continued:
            found.append((number, item.group(1) if item else stripped))
            continued = True
        else:
            found[-1] = (found[-1][0], f"{found[-1][1]} {stripped}")
    return found


def rules(path: str, text: str) -> list[Rule]:
    """The rules of one file: each sentence of its statements with enough words to compare."""
    found = []
    for line, statement in _statements(text):
        for sentence in SENTENCE_END.split(statement):
            tokens = WORD.findall(re.sub(r"n't\b", " not", sentence.lower()))
            words = frozenset(token for token in tokens if token not in STOPWORDS and token not in FORCE_WORDS)
            numbers = frozenset(token for token in words if token.isdigit())
            if len(words - numbers) >= MIN_WORDS:
                force = frozenset(FORCE_WORDS[token] for token in tokens if token in FORCE_WORDS)
                found.append(Rule(path, line, flat_text(sentence), words - numbers, force, numbers))
    return found


def _similarity(first: Rule, second: Rule) -> float:
    return len(first.words & second.words) / len(first.words | second.words)


def compare(first: Rule, second: Rule) -> dict[str, Any] | None:
    """The candidate two rules of different files make: a `conflict` when they share most of their words and differ
    in force or numbers, a `duplicate` when they share nearly all of them and agree; otherwise None."""
    similarity = _similarity(first, second)
    differs = sorted(first.force ^ second.force) + sorted(first.numbers ^ second.numbers)
    if similarity >= CONFLICT_SIMILARITY and differs:
        relation = "conflict"
    elif similarity >= DUPLICATE_SIMILARITY and not differs:
        relation = "duplicate"
    else:
        return None
    return {
        "relation": relation,
        "similarity": round(similarity, 2),
        "differs": differs,
        "paths": [first.path, second.path],
        "lines": [first.line, second.line],
        "texts": [_cut(first.text), _cut(second.text)],
    }


def _cut(text: str) -> str:
    return text if len(text) <= RULE_TEXT else text[: RULE_TEXT - 3].rstrip() + "..."


def overlaps(repository: str, texts: dict[str, str], sources: dict[str, Source]) -> list[dict[str, Any]]:
    """Every candidate pair of rules from two of a repository's files, with the reviewers given both files."""
    by_file = [rules(path, texts[path]) for path in sorted(texts)]
    found = []
    for index, first_rules in enumerate(by_file):
        for second_rules in by_file[index + 1 :]:
            for first in first_rules:
                for second in second_rules:
                    candidate = compare(first, second)
                    if candidate is None:
                        continue
                    shared = sorted(
                        name
                        for name, source in sources.items()
                        if first.path in source.hashes and second.path in source.hashes
                    )
                    found.append({"repository": repository, **candidate, "reviewers": shared})
    return found


def _overlap_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    return item["relation"] != "conflict", -item["similarity"], item["repository"], item["paths"], item["lines"]


def _resolved(base: str, reference: str) -> str | None:
    """A relative Markdown reference resolved against the directory `base` is in, or None when it leaves the
    repository."""
    parts: list[str] = list(PurePosixPath(base).parent.parts) if base else []
    for part in PurePosixPath(reference).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts) or None


def unsupplied(repository: str, texts: dict[str, str], sources: dict[str, Source]) -> list[dict[str, Any]]:
    """Each Markdown file a reviewer's prose links to or names in code that the reviewer was not given."""
    found: list[dict[str, Any]] = []
    for reviewer in sorted(sources):
        given = sources[reviewer].hashes
        for path in sorted(set(given) & set(texts)):
            for number, line in enumerate(texts[path].splitlines(), start=1):
                links = [_resolved(path, target) for target in LINK.findall(line) if "://" not in target]
                named = [(target, _resolved(path, target)) for target in CODE_REFERENCE.findall(line)]
                candidates = [link for link in links if link is not None and link not in given]
                candidates += [target for target, near in named if target not in given and near not in given]
                found += [
                    {"repository": repository, "reviewer": reviewer, "path": path, "line": number, "reference": target}
                    for target in dict.fromkeys(candidates)
                ]
    return found

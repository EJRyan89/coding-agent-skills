"""What the synthesis reads of each repository's reviewer guidance: the files each repository-scoped reviewer was built
from, the text of its prose files as that reviewer was given it, and the candidate overlaps a script can find in that
text before any model reads it.

A reviewer's files are the repository-relative paths in `review.adapter.source_hashes` of the latest record it wrote,
anywhere in the archive; a profile the suite ships is no file of the repository and is left out. Reviewers whose
records pinned different hashes of one file were given different versions of it, so each version is its own line with
its own reviewers, and its text is checked against the rules of the files those reviewers were given. A prose file's
text is read from the repository's configured checkout only when its SHA-256 matches the hash the records pinned: the
file in the working tree, read once however many versions and commits pinned it, or else the file at a pinning
record's `source_commit`; a working-tree copy that cannot be read is passed over for the commit's, and the report says
so once per file. A file whose text does not match, or that is too large or past the input's total budget, is listed
by path alone with the reason. A script, manifest, or other code file is listed by path, so a recommendation can name
it, and its text is never read.

The overlap candidates come from the text read, every verified file, including one listed without its text for the
budget: each rule (a list item's or paragraph's sentence) is compared with the rules of the repository's other files
by the words they share, leaving out the words that set a rule's force (must, should, may, never, a severity name)
and its numbers. Two rules that share most of their words and differ in those are a candidate `conflict`; two that
share nearly all of them and agree in those are a candidate `duplicate`. Only rules that share one of their rarer
words are compared, which two rules that alike always do (see _prefix), and a repository's search visits at most
CANDIDATE_BUDGET such pairs, so files near READ_BYTES cannot stall the report; past it, the remaining pairs are not
compared, and the repository is named as one whose search stopped, so no candidate there is not read as no overlap.
A reference to a Markdown file a reviewer was not given is an `unsupplied` candidate. The synthesis judges each
candidate; the script only finds them.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any

from flat_text import flat_text
from git_client import GitClient, GitError
from path_containment import refusal
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
# The most rule pairs one repository's overlap search visits, a pair met again through another shared word included:
# rules of near-identical words share every word, so without it two such files near READ_BYTES would stall the report.
CANDIDATE_BUDGET = 200_000
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


@dataclass(frozen=True)
class WorkingCopy:
    """The working tree's copy of a file and its SHA-256, or None, and why it could not be read when it could not."""

    content: bytes | None
    digest: str | None = None
    unreadable: str | None = None


VersionKey = tuple[str, str]


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
    # A fact line for each prose file whose working-tree copy could not be read.
    unreadable: list[str] = field(default_factory=list)
    # Each repository whose overlap search stopped at CANDIDATE_BUDGET with pairs it did not compare.
    stopped: list[str] = field(default_factory=list)


def reviewer_sources(
    repositories: list[str], records: Mapping[Path, Mapping[str, Any]]
) -> dict[str, dict[str, Source]]:
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


def guidance_files(repositories: list[str], records: Mapping[Path, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Each repository's guidance files: the source files of the latest record each repository-scoped reviewer
    wrote. A profile the suite ships is recorded beside them under its `suite:<name>` reference, but it is no file of
    the repository, which cannot change it, so it is left out."""
    return {
        repository: sorted({path for source in by_name.values() for path in source.hashes})
        for repository, by_name in reviewer_sources(repositories, records).items()
    }


def is_prose(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PROSE_SUFFIXES


def _working_copy(root: Path, path: str) -> bytes | None:
    """The working tree's copy of `path`, or None when it is no file inside the checkout, is reached through a link, or
    is too large to read."""
    if refusal(path, root) is not None:
        return None
    target = root.joinpath(*PurePosixPath(path).parts)
    if target.is_file() and target.stat().st_size <= READ_BYTES:
        return target.read_bytes()
    return None


def read_working_copy(checkout: Path, path: str) -> WorkingCopy:
    """The working tree's copy of `path` with its SHA-256; None when it is no file inside the checkout, is too large,
    or cannot be read, which the result says why."""
    if not safe_repository_path(path):
        return WorkingCopy(None)
    try:
        content = _working_copy(checkout.resolve(), path)
    except OSError as exc:
        return WorkingCopy(None, unreadable=flat_text(f"{type(exc).__name__}: {exc.strerror or exc}"))
    return WorkingCopy(content, None if content is None else hashlib.sha256(content).hexdigest())


def read_committed(checkout: Path, commit: str | None, path: str, digest: str) -> bytes | None:
    """The file's bytes at `commit` when their SHA-256 is `digest`; None otherwise, or when the path leaves the
    checkout."""
    if commit is None or not safe_repository_path(path):
        return None
    try:
        result = GitClient().run(["show", f"{commit}:{path}"], directory=checkout)
    except GitError:
        return None
    content = result.output_bytes()
    if result.returncode != 0 or len(content) > READ_BYTES or hashlib.sha256(content).hexdigest() != digest:
        return None
    return content


@dataclass(frozen=True)
class Reader:
    """How a guidance file's bytes are read: its working tree's copy, once per file, and its copy at a commit."""

    working: Callable[[Path, str], WorkingCopy] = read_working_copy
    committed: Callable[[Path, str | None, str, str], bytes | None] = read_committed


def _file_versions(by_name: dict[str, Source]) -> dict[VersionKey, list[tuple[str, Source]]]:
    """Each version of a repository's files, by path and the hash a reviewer's record pinned, with every reviewer
    given that version, the latest record first."""
    versions: dict[VersionKey, list[tuple[str, Source]]] = {}
    for name in sorted(by_name):
        source = by_name[name]
        for path, digest in source.hashes.items():
            versions.setdefault((path, digest), []).append((name, source))
    for given in versions.values():
        given.sort(key=lambda item: item[1].stamp, reverse=True)
    return versions


def read_guidance(
    repositories: list[str],
    records: Mapping[Path, Mapping[str, Any]],
    checkouts: Mapping[str, Path | None],
    reader: Reader | None = None,
) -> Guidance:
    """The guidance the synthesis reads: each repository's files, a line for each version of each with the reviewers
    given it and its text when it is prose, verified, and within the budget, and the overlap and unsupplied-reference
    candidates in the text read."""
    reader = reader or Reader()
    sources = reviewer_sources(repositories, records)
    guidance = Guidance(guidance_files(repositories, records))
    budget = TOTAL_CHARS
    for repository in sorted(sources):
        checkout = checkouts.get(repository)
        texts: dict[VersionKey, str] = {}
        copies: dict[str, WorkingCopy] = {}
        # Whether each version of a file whose working-tree copy could not be read was found at a pinning commit.
        found: dict[str, list[bool]] = {}
        for (path, digest), given in sorted(_file_versions(sources[repository]).items()):
            line: dict[str, Any] = {
                "kind": "guidance-file",
                "repository": repository,
                "path": path,
                "role": "prose" if is_prose(path) else "code",
                "reviewers": sorted(name for name, _ in given),
                "status": "listed",
                "content": None,
            }
            if is_prose(path):
                text = None
                if checkout is not None:
                    if path not in copies:
                        copies[path] = reader.working(checkout, path)
                    commits = [source.commit for _, source in given]
                    text = _verified_text(checkout, copies[path], commits, path, digest, reader)
                    if copies[path].unreadable is not None:
                        found.setdefault(path, []).append(text is not None)
                if text is not None:
                    texts[path, digest] = text
                line["status"] = _status(text, budget)
                if line["status"] == "included" and text is not None:
                    line["content"] = text
                    budget -= len(text)
            guidance.lines.append(line)
        guidance.unreadable += [
            f"GUIDANCE_UNREADABLE {repository} {flat_text(path)} fallback={_fallback(versions)} "
            f"{copies[path].unreadable}"
            for path, versions in found.items()
        ]
        candidates, stopped = overlaps(repository, texts, sources[repository])
        guidance.overlaps += candidates
        guidance.stopped += [repository] if stopped else []
        guidance.unsupplied += unsupplied(repository, texts, sources[repository])
    guidance.overlaps.sort(key=_overlap_rank)
    return guidance


def _verified_text(
    checkout: Path, copy: WorkingCopy, commits: list[str | None], path: str, digest: str, reader: Reader
) -> str | None:
    """The version's text: the working tree's copy when its SHA-256 is `digest`, or else the copy at the first commit
    a record that pinned it was built at whose SHA-256 is."""
    content = copy.content if copy.digest == digest else None
    for commit in dict.fromkeys(commits) if content is None else ():
        content = reader.committed(checkout, commit, path, digest)
        if content is not None:
            break
    try:
        return None if content is None else content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _fallback(found: list[bool]) -> str:
    """`commit` when every version of an unreadable working-tree copy was found at a pinning commit, `none` when no
    version was, and `partial` when some were."""
    return "commit" if all(found) else "partial" if any(found) else "none"


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


def _prefix(rule: Rule, order: Mapping[str, tuple[int, str]]) -> list[str]:
    """The rule's rarest words in `order`, as many as leave fewer than CONFLICT_SIMILARITY of its words out. Two rules
    that alike share at least that share of each one's words, so they share one of each one's rarest."""
    words = sorted(rule.words, key=order.__getitem__)
    return words[: len(words) - int(CONFLICT_SIMILARITY * len(words)) + 1]


def _visits(
    by_version: list[tuple[VersionKey, list[Rule]]], order: Mapping[str, tuple[int, str]]
) -> Iterator[tuple[VersionKey, VersionKey, Rule, Rule | None]]:
    """Each pair of rules from two files that share one of their rarest words, each time a word they share brings it
    up, with None in place of the second rule for a pair already met."""
    prefixes = [[_prefix(rule, order) for rule in found] for _, found in by_version]
    for index, (first_key, first_rules) in enumerate(by_version):
        for later in range(index + 1, len(by_version)):
            second_key, second_rules = by_version[later]
            if first_key[0] == second_key[0]:
                continue
            postings: dict[str, list[int]] = {}
            for position, prefix in enumerate(prefixes[later]):
                for word in prefix:
                    postings.setdefault(word, []).append(position)
            for first, prefix in zip(first_rules, prefixes[index], strict=True):
                seen: set[int] = set()
                for position in (item for word in prefix for item in postings.get(word, ())):
                    yield first_key, second_key, first, None if position in seen else second_rules[position]
                    seen.add(position)


def overlaps(
    repository: str, texts: dict[VersionKey, str], sources: dict[str, Source]
) -> tuple[list[dict[str, Any]], bool]:
    """Every candidate pair of rules from two of a repository's files, each file at every version read, with the
    reviewers given both versions; a candidate two versions share is one, naming every reviewer given it. Only rules
    that share one of their rarest words are compared, at most CANDIDATE_BUDGET visits in all, and the second value
    says whether the search stopped there with pairs left to visit."""
    by_version = [(key, rules(key[0], texts[key])) for key in sorted(texts)]
    frequency = Counter(word for _, found in by_version for rule in found for word in rule.words)
    order = {word: (count, word) for word, count in frequency.items()}
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    shared: dict[tuple[VersionKey, VersionKey], set[str]] = {}
    visits = _visits(by_version, order)
    for first_key, second_key, first, second in islice(visits, CANDIDATE_BUDGET):
        candidate = None if second is None else compare(first, second)
        if candidate is None:
            continue
        given = shared.setdefault(
            (first_key, second_key),
            {name for name, source in sources.items() if _given(source, first_key, second_key)},
        )
        key = (candidate["relation"], *candidate["paths"], *candidate["lines"], *candidate["texts"])
        known = found.setdefault(key, {"repository": repository, **candidate, "reviewers": []})
        known["reviewers"] = sorted({*known["reviewers"], *given})
    return list(found.values()), next(visits, None) is not None


def _given(source: Source, *versions: VersionKey) -> bool:
    return all(source.hashes.get(path) == digest for path, digest in versions)


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


def unsupplied(repository: str, texts: dict[VersionKey, str], sources: dict[str, Source]) -> list[dict[str, Any]]:
    """Each Markdown file a reviewer's prose, at the version it was given, links to or names in code that the reviewer
    was not given."""
    found: list[dict[str, Any]] = []
    for reviewer in sorted(sources):
        given = sources[reviewer].hashes
        for path in sorted(given):
            text = texts.get((path, given[path]))
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                links = [_resolved(path, target) for target in LINK.findall(line) if "://" not in target]
                named = [(target, _resolved(path, target)) for target in CODE_REFERENCE.findall(line)]
                candidates = [link for link in links if link is not None and link not in given]
                candidates += [target for target, near in named if target not in given and near not in given]
                found += [
                    {"repository": repository, "reviewer": reviewer, "path": path, "line": number, "reference": target}
                    for target in dict.fromkeys(candidates)
                ]
    return found

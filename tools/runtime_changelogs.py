"""List each runtime's changelog entries since the release the guidance audit last read that mention what this
repository uses, and each guidance page that moved or is gone.

Usage:
  python tools/runtime_changelogs.py [--summary FILE] [--timeout SECONDS]

The guidance record, .claude/skills/audit-repository/references/guidance.json, names each runtime's changelog, its
format, and `read_through`, the last release whose entries the guidance audit read; the maintainer moves it forward in
the pull request that records the audit. Every entry of a newer release, prereleases aside, is kept when it mentions a
term this repository uses, and the terms are read from the code at each run, never typed here:

  flag         each flag a command line the repository starts passes, from the builders
               tools/runtime_command_lines.py starts, given the runtime version each changelog was read through, each
               newer release it lists, and no version, so a flag the code passes only for some of them is one;
               matched in that runtime's changelog only
  variable     each variable of the environment those builders start a command line in, and each environment
               variable a Python module under deployer/, tools/, or a skill's scripts reads or sets:
               a key of `os.environ`, of a mapping named `env`, `environ`, or `environment`, or of a dict literal that
               spreads one, a name `getenv` takes, or a member of a module-level `*_VARIABLES` collection, each a
               string literal or a module-level string constant; and each `${NAME}` a skill's or agent's Markdown names
  path         each folder and file the deployer writes under the home, from deployer/paths.py's Paths, and the Codex
               metadata file it writes beside an adapter
  frontmatter  each key a skill's or agent's frontmatter holds, which the renderer carries into the adapters, and
               each key of the Codex metadata it writes

A flag, a path, and a variable or frontmatter key that holds a hyphen or an underscore match where the entry writes
them as a whole word, as after a slash or a parenthesis, in a code span, or, for a flag, before `=` and its value,
and never inside a longer flag, name, or path. A one-word frontmatter key matches only in a code span, and a one-word
variable only in a code span or as `$NAME`, `${NAME}`, `$env:NAME`, or `%NAME%`, since a key such as `model` and a
variable such as `OS` are also ordinary words.

Each page of the guidance record is requested, which covers every page the audit-skill and audit-ai-config provenance
files cite, since the audit-repository suite holds each of those to be in the record. Its text is never compared: a
page is reported only when it redirects or is gone.

It prints one fact per line:

  TERMS <kind> <count>                                   the terms of each kind read from the code
  CHANGELOG <subject> <read_through> <latest> <read> <kept>
                                                         the releases newer than the recorded one, the latest of
                                                         them or the recorded one, the entries read from them, and
                                                         the entries kept
  UNREAD <subject> <release> "<reason>"                  a newer release of which the watch read no entry, since its
                                                         text is empty or holds no bullet, quoting its first line, so
                                                         the readers read that release themselves
  ENTRY <subject> <release> "<terms>" "<entry>"          one kept entry, with the terms it mentions
  SAMPLE_BEHIND <subject> <sample> <recorded> <latest>   a sample of the runtime's output captured under
                                                         tests/fixtures/captured-output records a version older
                                                         than the changelog's latest release, so it is due to be
                                                         captured again (tools/captured_samples.py)
  MOVED <page> <address>                                 the page redirects to that address
  GONE <page> <status>                                   the page answers 404 or 410
  UNREADABLE <page> "<reason>"                           the page answers another error or cannot be reached
  FAILED <reason>                                        the watch could not run: the record cannot be read, names no
                                                         changelog for a runtime the deployer supports, a changelog
                                                         cannot be fetched or read or does not name the release its
                                                         read_through records, the code its terms come from cannot be
                                                         read, or the samples' provenance cannot be read; exit 1

With --summary FILE it also appends the findings to FILE as Markdown, as the workflow does with its step summary. A
finding is never a failure: the run exits 0 whatever it lists, and 1 only on FAILED. A usage error exits 2.

It is the check .github/workflows/runtime-changelogs.yml runs weekly. It decides nothing: the guidance audit after
each tag still judges each entry and page, starting from what the latest run lists. It starts no model, needs no
secret, and makes only unauthenticated requests, so it stays a scheduled signal and never a required check, since what
it lists changes whenever a runtime releases.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
import guidance_record
from agent_skills import CODEX_METADATA
from console import run_main
from flat_text import single_line
from guidance_record import GUIDANCE_PATH, PYTHON_SUBJECT, Changelog, GuidanceRecord, GuidanceRecordError
from runtime_support import RUNTIMES as SUBJECTS
from suite_names import is_suite_name

from deployer.paths import Paths
from deployer.render import CODEX_USER_ONLY
from tools import captured_samples, runtime_command_lines
from tools.runtime_canary import DECLARED_RUNTIME, REPOSITORY_ROOT, quote

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "coding-agent-skills-runtime-changelogs"
# The most pages of GitHub releases read for one changelog, a hundred releases each, prereleases among them.
MAX_RELEASE_PAGES = 10
# The pages requested at once.
PAGE_WORKERS = 8
GONE_STATUSES = (404, 410)
# The Python the variables are read from, from the top of the repository, suites aside.
PYTHON_ROOTS = ("deploy.py", "deployer", "tools", "skills")
# The Markdown whose `${NAME}` placeholders a runtime fills in, such as `${CLAUDE_SKILL_DIR}`.
MARKDOWN_GLOBS = ("skills/*/SKILL.md", "skills/*/*.md", "agents/*.md")
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
ENVIRONMENT_MAPPING = re.compile(r"(?:^|_)(?:environ|env|environment)$", re.IGNORECASE)
VARIABLE_COLLECTION = re.compile(r"(?:^|_)VARIABLES$")
# A release heading of a Markdown changelog, such as `## 2.1.302` or `## 1.0.103 - 2026-10-22`, and a release whose
# number goes on into a prerelease suffix, such as `1.0.104-0`.
RELEASE_HEADING = re.compile(r"^#{1,3}\s+\[?v?(?P<release>\d+(?:\.\d+)+)(?P<suffix>[-+][0-9A-Za-z.]+)?\b")
BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.*)$")
YAML_KEY = re.compile(r"^\s*(?P<key>[A-Za-z_][\w-]*):", re.MULTILINE)
KINDS = ("flag", "variable", "path", "frontmatter")


class WatchError(Exception):
    """The watch cannot run: its record, a changelog, or the code its terms come from cannot be read."""


@dataclass(frozen=True)
class Fetched:
    """What a request returned: its status, the address it ended at after redirects, and its body."""

    status: int
    url: str
    body: bytes = b""
    reason: str = ""


# Requests an address within a time limit and returns what it returned, an error status included, never raising.
Fetcher = Callable[[str, float], Fetched]


def fetch(url: str, timeout: float) -> Fetched:
    """GET `url`, following redirects, unauthenticated."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the record holds https only
            return Fetched(response.status, response.geturl(), response.read())
    except urllib.error.HTTPError as exc:
        return Fetched(exc.code, exc.geturl() or url, reason=f"HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Fetched(0, url, reason=str(getattr(exc, "reason", exc)))


@dataclass(frozen=True)
class Term:
    kind: str
    text: str
    # The guidance subject whose changelog alone it is matched in, or None for every changelog.
    subject: str | None = None

    @property
    def pattern(self) -> re.Pattern[str]:
        text = re.escape(self.text)
        one_word = not re.search(r"[-_]", self.text)
        if self.kind == "frontmatter" and one_word:
            return re.compile(rf"`{text}:?`")
        if self.kind == "variable" and one_word:
            return re.compile(rf"(?:`|\$\{{?|\$env:|%){text}(?![\w-])")
        if self.kind == "path":
            return re.compile(rf"(?<![\w.-]){text}(?![\w-])")
        return re.compile(rf"(?<![\w-]){text}(?![\w-])")


# --- the terms ----------------------------------------------------------------------------------------------------


def command_line_terms(versions: Mapping[str, Sequence[tuple[int, ...]]]) -> list[Term]:
    """Each flag of each command line runtime_command_lines.py builds, for the runtime it starts, and each variable
    of the environment it starts the line in, which holds the signed-out folders and the proxy it sets. A builder
    that reads the runtime's version is given each of `versions` for its changelog's subject, and no version, which it
    takes to be the latest, so a flag the code passes only before or after a release a changelog lists is a term.
    Where GitHub CLI sits beside Node.js, that check cannot hide it and builds no signed-out environment, so the
    variables are the ones the builders set alone."""
    terms: list[Term] = []
    with tempfile.TemporaryDirectory(prefix="runtime-changelogs-") as directory:
        for runtime, subject in DECLARED_RUNTIME.items():
            root = Path(directory) / runtime
            try:
                environment = runtime_command_lines.signed_out(root / "home", runtime)
            except runtime_command_lines.Unsafe:
                environment = {}
            lines = [
                line
                for index, version in enumerate([None, *versions.get(subject, ())])
                for line in runtime_command_lines.command_lines(
                    runtime, runtime, root / str(index), environment, version
                )
            ]
            flags = dict.fromkeys(flag for line in lines for flag in line.flags)
            variables = dict.fromkeys(
                name for line in lines for name in line.environment if VARIABLE_NAME.fullmatch(name)
            )
            terms += [Term("flag", flag, subject) for flag in flags]
            terms += [Term("variable", name) for name in variables]
    return terms


def python_files(top: Path) -> list[Path]:
    files: list[Path] = []
    for root in PYTHON_ROOTS:
        path = top / root
        found = [path] if path.is_file() else sorted(path.rglob("*.py"))
        files += [file for file in found if not is_suite_name(file.name) and "fixtures" not in file.parts]
    return files


def module_constants(tree: ast.Module) -> dict[str, str]:
    """The module-level names bound to one string literal."""
    constants: dict[str, str] = {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        target = node.target if isinstance(node, ast.AnnAssign) else target
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
    return constants


def terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_mapping(node: ast.expr) -> bool:
    name = terminal_name(node)
    return name is not None and ENVIRONMENT_MAPPING.search(name) is not None


def environment_keys(node: ast.AST) -> Iterator[ast.expr]:
    """The expressions `node` uses as an environment variable's name."""
    if isinstance(node, ast.Subscript) and is_mapping(node.value):
        yield node.slice
    elif isinstance(node, ast.Call) and node.args:
        func = node.func
        if (
            isinstance(func, ast.Attribute) and func.attr in {"get", "pop", "setdefault"} and is_mapping(func.value)
        ) or (terminal_name(func) in ("getenv", "putenv", "unsetenv")):
            yield node.args[0]
    elif isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
        if is_mapping(node.comparators[0]):
            yield node.left
    elif isinstance(node, ast.Dict) and any(
        key is None and is_mapping(value) for key, value in zip(node.keys, node.values, strict=True)
    ):
        yield from (key for key in node.keys if key is not None)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        name = terminal_name(target) if isinstance(target, (ast.Name, ast.Attribute)) else None
        if name and VARIABLE_COLLECTION.search(name) and isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            yield from node.value.elts


def module_variables(tree: ast.Module, constants: Mapping[str, str]) -> set[str]:
    """The environment variables a module reads or sets, by literal or by a string constant."""
    names: set[str] = set()
    for node in ast.walk(tree):
        for key in environment_keys(node):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
            elif isinstance(key, ast.Name) and key.id in constants:
                names.add(constants[key.id])
    return {name for name in names if VARIABLE_NAME.fullmatch(name)}


def variable_terms(top: Path) -> list[Term]:
    """Each environment variable the repository's Python reads or sets, and each one its Markdown has a runtime fill
    in. A constant is resolved across modules by its name, since one module defines what another imports."""
    trees: list[ast.Module] = []
    for path in python_files(top):
        try:
            trees.append(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            raise WatchError(f"cannot read {path.relative_to(top).as_posix()}: {exc}") from exc
    constants: dict[str, str] = {}
    for tree in trees:
        constants.update(module_constants(tree))
    names: set[str] = set()
    for tree in trees:
        names |= module_variables(tree, {**constants, **module_constants(tree)})
    for pattern in MARKDOWN_GLOBS:
        for path in top.glob(pattern):
            try:
                names.update(PLACEHOLDER.findall(path.read_bytes().decode("utf-8")))
            except (OSError, UnicodeDecodeError) as exc:
                raise WatchError(f"cannot read {path.relative_to(top).as_posix()}: {exc}") from exc
    return [Term("variable", name) for name in sorted(names)]


def path_terms(top: Path) -> list[Term]:
    """Each folder and file under the home a deployment writes, as the home-relative path a changelog writes."""
    home = Path(tempfile.gettempdir()) / "runtime-changelogs-home"
    paths = Paths(source_dir=top, home=home)
    found = {CODEX_METADATA}
    for name in dir(Paths):
        if isinstance(getattr(Paths, name), property):
            value = getattr(paths, name)
            if isinstance(value, Path) and value.is_relative_to(home) and value != home:
                found.add(value.relative_to(home).as_posix())
    for value in paths.managed_roots:
        found.add(value.relative_to(home).as_posix())
    return [Term("path", path) for path in sorted(found)]


def frontmatter_terms(top: Path) -> list[Term]:
    """Each key of a skill's or agent's frontmatter, and of the Codex metadata the deployer writes."""
    keys = set(YAML_KEY.findall(CODEX_USER_ONLY.decode("utf-8")))
    for path in [*sorted(top.glob("skills/*/SKILL.md")), *sorted(top.glob("agents/*.md"))]:
        try:
            keys.update(frontmatter.read(path).keys())
        except (OSError, frontmatter.FrontmatterError) as exc:
            raise WatchError(f"cannot read the frontmatter of {path.relative_to(top).as_posix()}: {exc}") from exc
    return [Term("frontmatter", key) for key in sorted(keys)]


def terms(top: Path, versions: Mapping[str, Sequence[tuple[int, ...]]] | None = None) -> list[Term]:
    """Every term, each once, in KINDS order, the flags built at `versions`, each subject's runtime versions."""
    found = [
        *command_line_terms(versions or {}),
        *variable_terms(top),
        *path_terms(top),
        *frontmatter_terms(top),
    ]
    unique = list(dict.fromkeys(found))
    return sorted(unique, key=lambda term: KINDS.index(term.kind))


# --- the changelogs -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    release: tuple[int, ...]
    entries: tuple[str, ...]
    # Why the watch read no entry of a release, quoting its first line of text, or "" when it read one.
    unread: str = ""

    @property
    def name(self) -> str:
        return ".".join(str(part) for part in self.release)


def release_number(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def bullet_entries(lines: Iterable[str]) -> list[str]:
    """Each bullet of Markdown, with the lines that continue it joined to it, on one line."""
    entries: list[list[str]] = []
    continuing = False
    for line in lines:
        bullet = BULLET.match(line)
        if bullet:
            entries.append([bullet.group("text")])
            continuing = True
        elif continuing and line.strip() and not line.lstrip().startswith("#"):
            entries[-1].append(line.strip())
        else:
            continuing = False
    return [text for text in (single_line(" ".join(parts)) for parts in entries) if text]


def read_release(release: tuple[int, ...], lines: Sequence[str]) -> Release:
    """A release with the entries of its text, and why none was read when its text holds no bullet: an empty text,
    or one in a shape bullet_entries does not read, such as prose or a numbered list."""
    entries = tuple(bullet_entries(lines))
    if entries:
        return Release(release, entries)
    text = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return Release(release, entries, f"no bullet in its text: {single_line(text[0])}" if text else "its text is empty")


def markdown_releases(text: str) -> list[Release]:
    """The releases of a Markdown changelog, prereleases aside, each with its entries."""
    releases: list[Release] = []
    current: tuple[int, ...] | None = None
    body: list[str] = []

    def close() -> None:
        if current is not None:
            releases.append(read_release(current, body))

    for line in text.splitlines():
        heading = RELEASE_HEADING.match(line)
        if heading:
            close()
            body = []
            current = None if heading.group("suffix") else release_number(heading.group("release"))
        elif current is not None:
            body.append(line)
    close()
    return releases


def github_releases(payload: object, url: str) -> list[Release]:
    """The releases of one page of GitHub's releases API, drafts and prereleases aside."""
    if not isinstance(payload, list):
        raise WatchError(f"{url} did not return a list of releases")
    releases: list[Release] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        found = re.search(r"(?<![\d.])(\d+(?:\.\d+)+)(?![\d.]|-)", str(item.get("tag_name", "")))
        if found:
            releases.append(read_release(release_number(found.group(1)), str(item.get("body") or "").splitlines()))
    return releases


def named(changelog: Changelog, releases: Sequence[Release], where: str) -> None:
    """WatchError unless `releases` holds the changelog's read_through: one it never names would keep every release
    up to it unread, and the watch would report none of them."""
    since = release_number(changelog.read_through)
    if not any(release.release == since for release in releases):
        raise WatchError(
            f"{where} names no release {changelog.read_through}, the read_through {GUIDANCE_PATH} records for "
            f"{changelog.subject}"
        )


def changelog_releases(changelog: Changelog, fetcher: Fetcher, timeout: float) -> list[Release]:
    """Every release `changelog` lists newer than its read_through, or WatchError when it cannot be read or does not
    name its read_through."""
    since = release_number(changelog.read_through)
    if changelog.format == "markdown":
        fetched = fetcher(changelog.url, timeout)
        if fetched.status != 200:
            raise WatchError(
                f"cannot fetch the {changelog.subject} changelog {changelog.url}: {fetched.reason or fetched.status}"
            )
        releases = markdown_releases(fetched.body.decode("utf-8", errors="replace"))
        if not releases:
            raise WatchError(f"the {changelog.subject} changelog {changelog.url} names no release")
        named(changelog, releases, f"the {changelog.subject} changelog {changelog.url}")
        return [release for release in releases if release.release > since]
    newer: list[Release] = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        url = f"{changelog.url}?per_page=100&page={page}"
        fetched = fetcher(url, timeout)
        if fetched.status != 200:
            raise WatchError(f"cannot fetch the {changelog.subject} releases {url}: {fetched.reason or fetched.status}")
        try:
            payload = json.loads(fetched.body)
        except ValueError as exc:
            raise WatchError(f"{url} did not return JSON: {exc}") from exc
        releases = github_releases(payload, url)
        newer += [release for release in releases if release.release > since]
        # Read on to the read_through itself, past an older release published after it, such as a backported fix.
        if any(release.release == since for release in releases):
            return newer
        if not payload:
            break
    raise WatchError(
        f"the {changelog.subject} releases name no release {changelog.read_through}, the read_through "
        f"{GUIDANCE_PATH} records, in the pages read, at most {MAX_RELEASE_PAGES}"
    )


def versions_read(changelog: Changelog, releases: Sequence[Release]) -> list[tuple[int, ...]]:
    """The runtime versions a run reads `changelog` across: the one it was read through and each newer release."""
    return [release_number(changelog.read_through), *(release.release for release in releases)]


def matching(entry: str, subject: str, watched: Sequence[Term]) -> list[str]:
    """The terms `entry` mentions, among those watched in `subject`'s changelog."""
    found = [term.text for term in watched if term.subject in (None, subject) and term.pattern.search(entry)]
    return list(dict.fromkeys(found))


def latest_release(changelog: Changelog, releases: Sequence[Release]) -> tuple[int, ...]:
    return max((release.release for release in releases), default=release_number(changelog.read_through))


def changelog_lines(changelog: Changelog, releases: Sequence[Release], watched: Sequence[Term]) -> list[str]:
    latest = latest_release(changelog, releases)
    ordered = sorted(releases, key=lambda release: release.release)
    unread = [
        f"UNREAD {changelog.subject} {release.name} {quote(release.unread)}" for release in ordered if release.unread
    ]
    entries: list[str] = []
    for release in ordered:
        for entry in release.entries:
            found = matching(entry, changelog.subject, watched)
            if found:
                entries.append(f"ENTRY {changelog.subject} {release.name} {quote(' '.join(found))} {quote(entry)}")
    read = sum(len(release.entries) for release in releases)
    head = f"CHANGELOG {changelog.subject} {changelog.read_through} {'.'.join(map(str, latest))} {read} {len(entries)}"
    return [head, *unread, *entries]


# --- the pages ----------------------------------------------------------------------------------------------------


def same_address(requested: str, reached: str) -> bool:
    return requested.split("#", 1)[0].rstrip("/") == reached.split("#", 1)[0].rstrip("/")


def page_line(url: str, fetched: Fetched) -> str | None:
    """What a page's request shows, or None for a page that answered where it was asked."""
    if fetched.status in GONE_STATUSES:
        return f"GONE {url} {fetched.status}"
    if fetched.status != 200:
        return f"UNREADABLE {url} {quote(fetched.reason or f'HTTP {fetched.status}')}"
    if not same_address(url, fetched.url):
        return f"MOVED {url} {fetched.url}"
    return None


def page_lines(urls: Sequence[str], fetcher: Fetcher, timeout: float) -> list[str]:
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        results = list(pool.map(lambda url: page_line(url, fetcher(url, timeout)), urls))
    return [line for line in results if line is not None]


# --- the run ------------------------------------------------------------------------------------------------------


def read_record(top: Path) -> GuidanceRecord:
    try:
        record = guidance_record.read(top / GUIDANCE_PATH, [*SUBJECTS, PYTHON_SUBJECT])
    except GuidanceRecordError as exc:
        raise WatchError(str(exc)) from exc
    missing = [subject for subject in SUBJECTS if subject not in {c.subject for c in record.changelogs}]
    if missing:
        raise WatchError(f"{GUIDANCE_PATH} names no changelog for {', '.join(missing)}")
    return record


def watch(top: Path, fetcher: Fetcher, timeout: float, watched: Sequence[Term] | None = None) -> list[str]:
    """Every line of a run over the checkout at `top`, or WatchError."""
    record = read_record(top)
    read = [(changelog, changelog_releases(changelog, fetcher, timeout)) for changelog in record.changelogs]
    if watched is None:
        watched = terms(top, {changelog.subject: versions_read(changelog, releases) for changelog, releases in read})
    try:
        samples = captured_samples.read(top, SUBJECTS)
    except captured_samples.SampleError as exc:
        raise WatchError(str(exc)) from exc
    lines = [f"TERMS {kind} {sum(1 for term in watched if term.kind == kind)}" for kind in KINDS]
    for changelog, releases in read:
        lines += changelog_lines(changelog, releases, watched)
    for changelog, releases in read:
        lines += captured_samples.behind(samples, changelog.subject, latest_release(changelog, releases))
    return lines + page_lines([page.url for page in record.pages], fetcher, timeout)


def markdown_cell(text: str) -> str:
    return text.replace("|", "&#124;")


def summary(lines: Sequence[str]) -> str:
    """The findings as Markdown: each runtime's kept entries, then the pages that moved or are gone."""
    rows = ["## Runtime changelogs and guidance pages", ""]
    unread = {line.split(" ")[1] for line in lines if line.startswith("UNREAD ")}
    for line in lines:
        if line.startswith("CHANGELOG "):
            _, subject, since, latest, read, kept = line.split(" ")
            rows += ["", f"### {subject}: releases after {since}, up to {latest}", "", f"Entries read: {read}.", ""]
            rows += ["No entry read mentions a watched term.", ""] if kept == "0" else []
            if kept != "0" or subject in unread:
                rows += ["| Release | Terms | Entry |", "| --- | --- | --- |"]
        elif line.startswith("UNREAD "):
            _, _, release, reason = line.split(" ", 3)
            rows.append(f"| {release} | none read | {markdown_cell(json.loads(reason))} |")
        elif line.startswith("ENTRY "):
            _, _, release, rest = line.split(" ", 3)
            decoder = json.JSONDecoder()
            found, end = decoder.raw_decode(rest)
            entry, _ = decoder.raw_decode(rest, end + 1)
            rows.append(f"| {release} | {markdown_cell(found)} | {markdown_cell(entry)} |")
    behind = [line for line in lines if line.startswith("SAMPLE_BEHIND ")]
    rows += ["", "### Captured samples", ""]
    rows += (
        ["| Runtime | Sample | Recorded | Latest |", "| --- | --- | --- | --- |"]
        + [f"| {' | '.join(line.split(' ')[1:])} |" for line in behind]
        if behind
        else ["No captured sample is older than its runtime's latest release."]
    )
    pages = [line for line in lines if line.startswith(("MOVED ", "GONE ", "UNREADABLE "))]
    rows += ["", "### Guidance pages", ""]
    rows += (
        [f"- {markdown_cell(line)}" for line in pages] if pages else ["Every page answered where the record names it."]
    )
    return "\n".join(rows) + "\n"


def main(arguments: list[str] | None = None, fetcher: Fetcher = fetch, top: Path = REPOSITORY_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--summary", type=Path, metavar="FILE", help="append the findings to FILE as Markdown")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    namespace = parser.parse_args(arguments)
    try:
        lines = watch(top, fetcher, namespace.timeout)
    except WatchError as exc:
        print(f"FAILED {single_line(str(exc))}")
        return 1
    for line in lines:
        print(line)
    if namespace.summary is not None:
        with namespace.summary.open("a", encoding="utf-8") as stream:
            stream.write(summary(lines))
    return 0


if __name__ == "__main__":
    run_main(main)

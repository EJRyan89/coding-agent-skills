"""The silent-revert guard: a pull request that returns a file to an older state of main says so in its body.

A branch rebased or merged badly, or a conflict resolved by taking the branch's side of a file it never set out to
change, carries that file's older contents, and its diff against main undoes a commit that merged meanwhile. The
reverted code was green before, so every other check passes. This guard compares each file the pull request changes
with the file's versions in main's history and names each one whose content equals the state before a commit on main,
with that commit and its pull request, unless the body declares the revert.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from markdown_sections import HTML_COMMENT
from validation_support import REPOSITORY_ROOT, PullRequestChecks, git

from tools.release_notes import RELEASE_BRANCH, SQUASH_TITLE_NUMBER

# How far before the merge base the walk of main's first-parent line reaches, in commits. In continuous integration
# the checkout is the merge of the pull request into main, so its merge base is main's tip and this window is all the
# history the guard sees; locally the commits since the branch's own merge base come on top. A stale copy of a file is
# as old as the branch's last look at main, and main merges a few dozen pull requests a day at its busiest, so two
# hundred commits reach back several days of a branch left behind, while a match further back is ever more likely a
# deliberate return to an old text than a stale one.
COMMITS_BEFORE_MERGE_BASE = 200
# A declaration, on a line of its own, optionally a list item, anywhere in the body outside an HTML comment:
# "Reverts `<path>` to its state before <commit>", the commit by seven or more hexadecimal digits of its name.
DECLARATION = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?Reverts `(?P<path>[^`\n]+)` to its state before (?P<commit>[0-9a-fA-F]{7,64})\b",
    re.MULTILINE,
)
DECLARATION_SHAPE = "Reverts `<path>` to its state before <commit>"
ABSENT = "0" * 40
# Paths per git call, well inside the longest command line Windows starts.
PATH_BATCH = 100


@dataclass(frozen=True)
class Revert:
    """A file the pull request returns to its state before a commit on main."""

    path: str
    commit: str
    subject: str

    @property
    def pull_request(self) -> str | None:
        match = SQUASH_TITLE_NUMBER.search(self.subject)
        return f"#{match.group(1)}" if match else None

    def report(self) -> str:
        source = f"pull request {self.pull_request}" if self.pull_request else "no pull request in its title"
        return (
            f"{self.path}: the pull request returns it to its state before {self.commit[:12]} ({source}: "
            f"{self.subject}), undoing that commit's change to it; if the revert is intended, declare it on a line "
            f"of the pull request body as `Reverts `{self.path}` to its state before {self.commit[:12]}` (a local "
            "run reads the body from --pr-body <file>, and otherwise the branch's commit messages); otherwise bring "
            f"the file back to {RELEASE_BRANCH}'s version"
        )


def _git(root: Path, *arguments: str) -> str:
    """git's output, or ValueError naming the command when it fails."""
    completed = git(root, "--literal-pathspecs", *arguments)
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(arguments[:2])} failed: {completed.stderr.strip()}")
    return completed.stdout


def _batches(paths: list[str]) -> Iterable[list[str]]:
    return (paths[start : start + PATH_BATCH] for start in range(0, len(paths), PATH_BATCH))


def _raw_entries(tokens: list[str]) -> Iterable[tuple[str, str, str, str]]:
    """Each (old blob, new blob, status, path) of the NUL-separated --raw entries after a header."""
    for meta, path in zip(tokens[0::2], tokens[1::2], strict=False):
        fields = meta.strip().split()
        if len(fields) == 5 and fields[0].startswith(":"):
            yield fields[2], fields[3], fields[4], path


def changed_blobs(root: Path, base: str, head: str) -> dict[str, str]:
    """Each file the head changes against base that it still holds, with the head's blob: a deletion leaves nothing
    to compare."""
    output = _git(root, "diff", "-z", "--no-renames", "--raw", "--no-abbrev", base, head)
    return {path: new for _, new, status, path in _raw_entries(output.split("\0")) if status != "D" and new != ABSENT}


def current_blobs(root: Path, ref: str, paths: list[str]) -> dict[str, str]:
    """Each path's blob at ref, leaving out a path ref does not hold."""
    blobs: dict[str, str] = {}
    for batch in _batches(paths):
        for line in _git(root, "ls-tree", "-z", ref, "--", *batch).split("\0"):
            meta, _, path = line.partition("\t")
            if path and meta.split()[1:2] == ["blob"]:
                blobs[path] = meta.split()[2]
    return blobs


def window_start(root: Path, merge_base: str) -> str | None:
    """The commit COMMITS_BEFORE_MERGE_BASE first-parent steps before the merge base, or None when the history is
    shorter, so the walk reaches the first commit."""
    found = _git(root, "rev-list", "--first-parent", f"--skip={COMMITS_BEFORE_MERGE_BASE}", "--max-count=1", merge_base)
    return found.strip() or None


def earlier_states(root: Path, main: str, start: str | None, paths: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    """For each path, newest first, each (blob before, commit, subject) of the commits on main's first-parent line
    after start that changed it."""
    states: dict[str, list[tuple[str, str, str]]] = {}
    span = f"{start}..{main}" if start else main
    for batch in _batches(paths):
        output = _git(
            root,
            "log",
            "-z",
            "--first-parent",
            "--diff-merges=first-parent",
            "--no-renames",
            "--raw",
            "--no-abbrev",
            "--format=%x01%H%x02%s",
            span,
            "--",
            *batch,
        )
        for record in output.split("\x01")[1:]:
            header, *tokens = record.split("\0")
            commit, _, subject = header.partition("\x02")
            for old, _, _, path in _raw_entries([token.lstrip("\n") for token in tokens]):
                states.setdefault(path, []).append((old, commit, subject))
    return states


def find_reverts(root: Path, head: str = "HEAD", main: str = RELEASE_BRANCH) -> list[Revert]:
    """Each file head changes against its merge base with main whose content equals the state before a commit on
    main's first-parent line, from main's tip back to COMMITS_BEFORE_MERGE_BASE commits before that merge base, and
    differs from main's current version. The tree is compared, never the branch's history, so a branch that merged
    main in is judged by what it holds. The newest such commit is the one named."""
    merge_base = _git(root, "merge-base", head, main).strip()
    changed = changed_blobs(root, merge_base, head)
    current = current_blobs(root, main, sorted(changed))
    candidates = sorted(path for path, blob in changed.items() if current.get(path) != blob)
    if not candidates:
        return []
    states = earlier_states(root, main, window_start(root, merge_base), candidates)
    reverts = []
    for path in candidates:
        match = next(((commit, subject) for old, commit, subject in states.get(path, []) if old == changed[path]), None)
        if match is not None:
            reverts.append(Revert(path, *match))
    return reverts


def declarations(text: str) -> set[tuple[str, str]]:
    """Each (path, commit prefix) the text declares a revert of, outside HTML comments, the commit in lower case."""
    visible = HTML_COMMENT.sub("", text.replace("\r\n", "\n"))
    return {(match["path"], match["commit"].lower()) for match in DECLARATION.finditer(visible)}


def is_declared(revert: Revert, declared: set[tuple[str, str]]) -> bool:
    return any(path == revert.path and revert.commit.startswith(commit) for path, commit in declared)


def silent_revert_problems(root: Path, body: str | None = None) -> list[str]:
    """Report each file HEAD returns to an older state of main that no declaration names.

    The declarations are read from the pull request body when given, since the body becomes the squash commit's
    message, and otherwise from the messages of the branch's commits since its merge base, so a local run reads them.
    """
    if git(root, "rev-parse", "--verify", "--quiet", f"{RELEASE_BRANCH}^{{commit}}").returncode != 0:
        return [
            f"{RELEASE_BRANCH} is missing, so the silent-revert check cannot compare the change with main's history; "
            "fetch it with `git fetch origin main`"
        ]
    if git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return [
            "this clone is shallow, so the silent-revert check cannot read main's history; fetch it with "
            "`git fetch --unshallow`"
        ]
    try:
        reverts = find_reverts(root)
        if not reverts:
            return []
        if body is None:
            merge_base = _git(root, "merge-base", "HEAD", RELEASE_BRANCH).strip()
            body = _git(root, "log", "--format=%B", f"{merge_base}..HEAD")
    except ValueError as error:
        return [f"the silent-revert check cannot read the history: {error}"]
    declared = declarations(body)
    return [revert.report() for revert in reverts if not is_declared(revert, declared)]


class SilentRevertPolicies(PullRequestChecks):
    def test_the_pull_request_returns_no_file_to_an_older_state_of_main_unless_its_body_declares_it(self) -> None:
        self.assertEqual([], silent_revert_problems(REPOSITORY_ROOT, self.pull_request.body_text()))

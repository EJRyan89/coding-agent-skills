"""Review one document from its file path, or each document a pull request changes, as one-file fixture canaries of
ejryan89-code-review-core.

Every command prints one fact per line:
  build   Writes a fixture directory (see review_canary.py in ejryan89-code-review-core) that holds the document as
          one change, and prints DOCUMENT <path in the fixture>, BASE committed <commit> | BASE none <reason>,
          ROUTE design-review | ROUTE generic, FIXTURE <fixture directory>, and OUTPUT <output directory>. Given a
          pull request, it first prints PULL <owner/repo#number> base <commit> head <commit>, then that block for each
          document it reviews, each in its own fixture, and SKIPPED <path> <reason> for a changed document it cannot.
          Its last line is PREPARE_ARGUMENTS --canary --fixture "<fixture directory>" ..., one --fixture for each
          fixture in the order of the FIXTURE lines, each path in forward-slash form: the arguments
          ejryan89-code-review-core's prepare takes after --host, passed as printed.
  resolve Finds the document a user named by its file name alone, under the working directory, by the rule below, and
          prints FOUND <path> for the one file found, CANDIDATE <path> for each of several, or for the one found when a
          folder could not be searched, NOT_FOUND <name> for none, and before them UNSEARCHED <path> <reason> for each
          folder or entry that could not be searched, and for the folder its bound stopped it at. Each path is
          relative to the working directory. It exits 1 when
          it printed an UNSEARCHED line, since what it found may not be all there is.
  report  Given --runs and the run list prepare printed, copies the report `finalize` recorded of each recorded run
          into the output directory beside the fixture the run list names for it, once its record names the review of
          that fixture's document, and prints REPORT <copy> and RECORD <the record JSON beside the original> for each,
          in the order prepare published the runs. For a listed run it cannot report, as one whose review failed or
          that is not finalized, or one whose fixture or report is not what the run list says, it prints UNREPORTED
          <selector> <run directory> <reason> in its place and goes on. Given --fixture and --report instead, it does
          the same for that one fixture and report, failing with the reason. A copy is never written over anything
          already there; a regular file it can read already holding the same bytes is the earlier copy, and is reported
          again, and anything else, a symbolic link or junction included whatever it leads to, is refused. A copy is
          made whole or not at all: one that fails part way leaves no report.md, so the next call makes it. It is put in
          place by a hard link, or, on a file system with none, such as FAT or exFAT, by an empty report.md reserved
          under its name and then replaced by the whole copy, so a reader may meet an empty report.md until the copy
          lands. A copy a kill stops there leaves that empty report.md, which the next call refuses, saying it is empty
          and to delete it and run report again. Once the copy is in place, report.md is whole: a temporary
          copy beside it that cannot then be removed fails with a reason saying report.md is made and naming the copy
          left, and the next call reports report.md as the earlier copy.

Exit status:
  0  the command printed its facts
  1  a last line FAILED <reason>, from resolve, an UNSEARCHED line for what it could not search, or, from report,
     an UNREPORTED line for a run it could not report
  2  a usage error

For a path, the head is the file as it is on disk, refused over the MAX_CHANGED_FILE_BYTES the snapshot reads of a
changed file. The base is the version committed at HEAD when the file lies in a git checkout and is committed there,
under the name git tracks it by, so the review covers the uncommitted change; otherwise, or with `--base none`, the base
has no such file and the whole document is reviewed. A failure git reports reading the commit or the file fails the
build with git's reason.

A pull request is named as owner/repo#number or by its URL, and validated as review-prs validates its --pull selectors.
It is read as review-prs reads one, so a pull request closed without being merged fails, with `FAILED Pull request is
closed without being merged`, and an open or merged one is reviewed. Its changed files are read from GitHub, through
ejryan89-code-review-core's GitHubClient and with no checkout, and those the design reviewer reviews (DESIGN_SUFFIXES,
or a document format) are kept, or the one `--document` names. The listing is the review core's, `list_pull_files`, and
is refused as `prepare` refuses it: when it is shorter than the files the pull request changes, as GitHub's files API
cuts it, and when the pull request's head moved while it was read, before its pages or after them, or its base was
retargeted by the end of them. Each kept document's head is its blob at the pull request's head commit, and its base its
blob at the merge base GitHub diffs the pull request against, under its old path when it was renamed; a document the
pull request adds has no base, and one GitHub answers as a symbolic link or a submodule is skipped. The fixture's
pull.json names the real pull request, so the record's URL is its URL, and the commits; the pull request's title,
description, branch names, and comments are never read into it, so the only text from GitHub a reviewer reads is the
documents' own. Nothing is written to GitHub.

A Word document or a PDF, a format `review_documents.document_format` names, is written to the fixture as its exact
bytes, so the fixture canary's snapshot extracts its text as it does a pull request's; one whose text would not be
extracted, or whose lines of text the change leaves as they were, labels aside, is refused here first. A Markdown,
plain-text, Word, or PDF document goes to the suite's design-review specialist, through a specialists manifest in the
fixture's base tree, and pull.json records its `--stage`, one of ejryan89-code-review-core's DOCUMENT_STAGES and review
by default, whose fixed sentence prepare puts in that specialist's prompt; any other text file goes to the generic
reviewer, and a stage given for one is refused. The default output is a new temporary directory, so nothing is
written to a checkout unless `--output` names a folder inside one; `--output` must name a new or empty directory, so
nothing already there is mixed with the review's files or replaced. On a FAT, exFAT, or SMB volume, macOS writes an
AppleDouble `._<name>` sidecar beside each file and folder written there, which the fixture canary's walk of the two
trees leaves out (ejryan89-skill-core's `apple_double.is_sidecar`), so it is never a document.

`resolve` takes a file name alone, with no folder, drive, or colon in it, and finds the files a program opens under that
name: a file of that name in the working directory itself is the one found, and only otherwise is the tree below it
searched, through ejryan89-skill-core's `folder_walk`, so its rule is the walk's. A version-control folder (`.git`,
`.hg`, `.svn`), a dependency folder or its cache (`node_modules`, `.venv`, `venv`, `.tox`, `.nox`, `__pycache__`), build
output (`bin`, `obj`), Visual Studio's `.vs`, another checkout below the working directory (a clone, submodule, or
worktree), and a linked folder are not searched; nor is a hidden folder, one whose name starts with a dot, which holds a
tool's state or cache, such as `.pytest_cache` or `.idea`, rather than a draft named by its file name: the walk prunes
it, so nothing inside it is listed. A matching entry that is a link, not a plain file, is passed over, since it may lead
out of the tree, and one whose own metadata cannot be read is printed as UNSEARCHED, never taken for no file there.
Names are compared as the folder holding each file compares them, so `Design.md` is found for `design.md` only where
that folder folds case. A `.gitignore` is not read: a folder it ignores is searched unless the rule above skips it, so a
draft kept in an ignored folder is still found. A folder or entry the walk could not list or examine, and a folder whose
name holds a line break or a control character, which no line could name, is printed as UNSEARCHED, and a single match
found beside one is printed as a CANDIDATE, since the folder not searched may hold another.

The walk is bounded: it lists at most 10000 folders (RESOLVE_FOLDER_LIMIT), the working directory included, so a search
started in a large folder, such as a home folder, ends. Past the bound it stops and prints UNSEARCHED for the next
folder it would have listed, which, with every folder after it in the walk's order, was not searched, and a single match
found before it is printed as a CANDIDATE.

Every read of a path goes through ejryan89-skill-core's `tree_reader`, bound to the tree it reads, which judges the path
by its text and its links before touching it: the document build names, once its path is resolved, through a reader
bound to the folder holding it, a document over the size a review reads refused by its status, unread; the checkout
around it through one bound to that checkout, so a tracked spelling of its name whose link leads out of the checkout is
never followed to compare it; a path in a fixture through one bound to that fixture's tree, so one that a link on its
way leads out of is refused, `<path> is not a path a fixture can hold: <reason>` with the reader's reason, before
anything is written; and the working directory resolve searches through one bound to it, so a linked folder or file
leading out of it is never followed, not even to learn what it is. Deciding whether a path lies in its tree asks the
tree's own folders and the folders above it how they compare names, which lists them, as the reader's limits state; the
walk's bound counts none of those listings. The reads outside those trees by design are each stated in
TREE_READ_ALLOWED with why: the document path the user names and the checkout git names around it, resolved before a
reader is bound, the --output folder and the temporary folder the skill writes its own files in, and the fixture and
the run folder report copies a report from; the run list report --runs reads through ejryan89-code-review-core, which
the policy does not see, is stated in the comment above that table.
"""

from __future__ import annotations

import argparse
import contextlib
import filecmp
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import file_presence
import json_reader
import json_writer
import private_folder
from atomic_file import TRACKED_FILE_MODE, create_bytes
from console import run_main
from flat_text import LINE_UNSAFE, flat_text, single_line
from folder_walk import BOUNDED, UNEXAMINED, UNLISTABLE, Walk, is_hidden
from git_client import GitClient, GitError, output_line
from github_client import GitHubError
from link_probe import is_plain_file
from path_containment import resolved_path, within
from path_keys import name_key
from review_batches import load_run_list
from review_config import LOCAL_OWNER
from review_documents import ExtractionRefused, document_format, extract, unlabeled
from review_github import GitHubClient, NotOneFile
from review_io import PersistenceError, working_path
from review_operation import parse_pull_selector
from review_run import PipelineError
from review_runtime import (
    DEFAULT_DOCUMENT_STAGE,
    DOCUMENT_STAGES,
    HELD_TEXT_FOLDER,
    MAX_CHANGED_FILE_BYTES,
    PathRoom,
    blob_matches,
    is_binary,
    path_room,
    unsafe_path_reason,
)
from review_specialists import reviewable_path
from tree_reader import TreeReader, TreeRefused

# The documents the design-review specialist reviews: Markdown and plain-text markup, matched by suffix, ignoring case.
DESIGN_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})
ROUTED_KINDS = "a Markdown, plain-text, Word, or PDF document"
# A pull request's URL, as GitHub shows it; anything after its number, such as /files, is refused.
PULL_URL = re.compile(r"(?i:https://github\.com)/([^/]+/[^/]+)/pull/([0-9]+)/?")
PULL_URL_FORM = "https://github.com/<owner>/<repo>/pull/<number>"
# Where the fixture keeps its specialists manifest, in both trees, so the manifest is never part of the change.
MANIFEST_PATH = ".review-document/specialists.json"
FINDING_CATEGORIES = ["requirement-gap", "alternative-unstated", "risk", "open-question", "inconsistency", "rollout"]
# The fixture's repository for a file outside a checkout, under the local owner review_config owns.
LOOSE_REPOSITORY = f"{LOCAL_OWNER}/document"
REPORT_NAME = "report.md"
# The most folders `resolve` lists, the working directory included, before it stops and says where.
RESOLVE_FOLDER_LIMIT = 10_000
# What a double-quoted argument does not keep as it is in Bash or PowerShell, so an output directory whose path holds
# one cannot be named in the PREPARE_ARGUMENTS line.
UNQUOTABLE = frozenset('"$`\\')
# The fields of a fixture's pull.json a record keeps under its pull_request, which together name the reviewed document.
REVIEWED_FIELDS = ("number", "title", "base_ref", "head_ref")


LINE_SPLIT_ALLOWED = {
    "refuse_unquotable": (
        "refuses a folder name holding any character str.splitlines breaks at, which is stricter than either shell's "
        "line end: such a name would break the line that quotes it somewhere"
    ),
}

# The presence reads and removals here that read a path they cannot stat, list, or remove as nothing there,
# each with why that is the answer it should give.
PRESENCE_ALLOWED = {
    "output_root": "removes the folder it made, part way filled, before raising the failure that stopped it, which an "
    "error of the removal must not replace",
}

# The reads outside a tree a reader is bound to, each with why it lies outside: the module states which reads are
# bound to which tree. One more is stated here, since the tree read policy cannot sanction it: report_runs reads the run
# list prepare printed through ejryan89-code-review-core's review_batches.load_run_list, which reads it with
# review_io.read_json, and the policy derives no reader from a sibling skill's scripts loaded through sibling_scripts,
# its stated limit, so an entry for report_runs would sanction nothing it sees. The run list is the pipeline's own state
# in the run folder, outside every tree the skill examines by design.
TREE_READ_ALLOWED = {
    "<module>": (
        "puts ejryan89-code-review-core's scripts on sys.path: skill_roots.sibling_scripts finds the sibling skill's "
        "scripts folder beside this skill's own, no path of a tree the skill examines"
    ),
    "given_path": (
        "resolves, its links followed, the document path the user names and the checkout git names around it, before "
        "a reader is bound to either, since each chooses the tree a reader is then bound to"
    ),
    "_is_a_path": (
        "whether anything is at the path build is given, before it is read as a document's path or a pull request's "
        "selector, so before any tree is chosen"
    ),
    "output_directory": (
        "judges the --output folder the user names for the skill's own files new or empty before anything is written: "
        "the skill's own output, wherever the user puts it"
    ),
    "output_root": (
        "resolves the new temporary folder the skill makes for its own files under the private temporary directory: "
        "the skill's own output"
    ),
    "report": (
        "the fixture build wrote, which --fixture or the run list names, and the report finalize recorded in the run "
        "folder under the home, each read where it is named, to copy the report beside the fixture: the skill's own "
        "output and the pipeline's, no tree the skill examines"
    ),
    "_is_file": "whether a fixture's pull.json, a recorded report, or its record is a file: the files report copies",
    "json_object": "a fixture's pull.json or a recorded report's record, which report compares: the files it copies",
    "earlier_copy": "compares a report.md beside the fixture with the recorded report it would copy there",
    "is_empty_file": "whether a report.md beside the fixture is the empty reservation a stopped copy left",
    "folded": (
        "name_key given no folder folds a name as text, composed and case-folded fully, and asks no folder; the policy "
        "counts every call of it, since given a folder it asks that folder how it compares names"
    ),
}


class DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class Base:
    """What the document is compared with: its committed bytes at `commit`, or nothing, for `reason`."""

    content: bytes | None
    commit: str | None
    reason: str


@dataclass(frozen=True)
class Location:
    """Where the document is: its path in the fixture, and the checkout holding it, if any."""

    relative: str
    checkout: Path | None


@dataclass(frozen=True)
class Pull:
    """The pull request whose documents are reviewed, by the commits its documents are read at: the merge base GitHub
    diffs it against, and its head."""

    repository: str
    number: int
    base: str
    head: str


@dataclass(frozen=True)
class Change:
    """A file a pull request changes, as its file listing names it."""

    path: str
    status: str
    previous: str | None  # the path it had at the base, for a renamed or copied file


@dataclass(frozen=True)
class Document:
    """A document a fixture holds: its path, its head's bytes, its format, and its base."""

    relative: str
    content: bytes
    form: str | None
    base: Base


def over_limit(name: str) -> DocumentError:
    """The refusal of a document over the size the snapshot reads of a changed file, which it would leave out."""
    return DocumentError(f"{name} is over the {MAX_CHANGED_FILE_BYTES // (1024 * 1024)} MiB a review reads")


def given_path(path: str | Path) -> Path:
    """`path`, a path the user names or the checkout git names, made absolute with its links followed, as
    TREE_READ_ALLOWED states, so a reader bound to the folder it names judges no link on its way again."""
    return resolved_path(path)


def document_bytes(path: Path, form: str | None) -> bytes:
    """The document's bytes at `path`, already resolved, as `checked_content` keeps them, read through a reader bound
    to the folder holding it. Refused when it is not a file or is over the size a review reads, which its status
    shows before it is read. A path with nothing at it is refused as missing, something else as not a file, and one a
    stat cannot examine with the stat's reason."""
    reader = TreeReader(path.parent)
    if reason := reader.missing_reason(path):
        raise DocumentError(f"{path} {reason}")
    if reader.status(path).st_size > MAX_CHANGED_FILE_BYTES:
        raise over_limit(str(path))
    content = reader.contents(path)
    if len(content) > MAX_CHANGED_FILE_BYTES:
        raise over_limit(str(path))
    return checked_content(str(path), content, form)


def checked_content(name: str, content: bytes, form: str | None) -> bytes:
    """A document's bytes: a text file's with its line endings as LF, or a Word document's or a PDF's exactly, once
    its text extracts to something to review. Refused, naming the document as `name`, when it is empty, binary, or a
    document whose text is not extracted."""
    if not content.strip():
        raise DocumentError(f"{name} is empty")
    if form is not None:
        if not document_text(name, form, content).strip():
            raise DocumentError(f"{name} holds no text to review")
        return content
    if is_binary(content):
        raise DocumentError(f"{name} is not a text file, a Word document, or a PDF")
    return normalized(content)


def document_text(name: str, form: str, content: bytes) -> bytes:
    """A document's text as the snapshot extracts it, refused for the reason the snapshot would give."""
    try:
        return extract(form, content)
    except ExtractionRefused as exc:
        raise DocumentError(f"{name} is a document whose text could not be extracted: {exc}") from exc


def normalized(content: bytes) -> bytes:
    """Line endings as LF, so a checkout that converts them on checkout never makes every line a change."""
    return content.replace(b"\r\n", b"\n")


def locate(path: Path, git: GitClient) -> Location:
    """The document's path in its checkout, as git tracks it, or its file name with no checkout. A path through the
    checkout's `.git` folder is refused in any spelling the checkout's file system takes for it, so `.GIT` is refused
    where the checkout folds case. The checkout is read through a reader bound to it."""
    try:
        top = git.rev_parse_path("--show-toplevel", directory=path.parent)
    except GitError as exc:
        raise DocumentError(f"git could not read the checkout around {path}: {exc}") from exc
    if top is None:
        return Location(path.name, None)
    checkout = given_path(top)
    reader = TreeReader(checkout)
    relative = PurePosixPath(*path.relative_to(checkout).parts)
    if reader.key(checkout / relative.parts[0]) == reader.key(checkout / ".git"):
        raise DocumentError(f"{path} is inside the checkout's .git folder")
    return Location(tracked_name(relative.as_posix(), path, reader, git), checkout)


def same_file(reader: TreeReader, candidate: Path, path: Path) -> bool:
    """Whether `candidate` and `path` lead to one file, each judged by `reader` first, so a name whose way leads out
    of its tree, or that cannot be examined, is not the file."""
    try:
        return os.path.samestat(reader.status(candidate), reader.status(path))
    except OSError:
        return False


def tracked_name(relative: str, path: Path, reader: TreeReader, git: GitClient) -> str:
    """The name git tracks the file at `path` by: `relative` itself, or the one spelling of it, differing only in
    case, that the index holds for the same file, as a file system that ignores case leaves a file renamed only by
    case. `relative` when git tracks neither. Each spelling is looked at through `reader`, bound to the checkout, so a
    tracked link leading out of it is never followed to compare it."""
    checkout = reader.tree
    listed = git.output(["ls-files", "-z", "--full-name", "--", f":(icase,literal){relative}"], directory=checkout)
    names = [name for name in listed.split("\0") if name]
    if relative in names:
        return relative
    same = [name for name in names if same_file(reader, checkout.joinpath(*PurePosixPath(name).parts), path)]
    if len(same) > 1:
        raise DocumentError(f"{relative} is tracked under several names that differ only in case: {', '.join(same)}")
    return same[0] if same else relative


def git_failure(exc: GitError, doing: str) -> GitError:
    """A git command's failure, keeping its class and exit code, said as what could not be done."""
    return GitError(f"git could not {doing}: {exc}", kind=exc.kind, returncode=exc.returncode)


def unborn(checkout: Path, git: GitClient) -> bool:
    """Whether HEAD names a branch that has no commit yet, as before a checkout's first commit. A detached HEAD is
    none; any git failure raises GitError."""
    branch = git.quiet_output(["symbolic-ref", "--quiet", "HEAD"], directory=checkout)
    return branch is not None and git.commit_of(output_line(branch), directory=checkout) is None


def committed_blob(commit: str, relative: str, checkout: Path, git: GitClient) -> str | None:
    """The id of the file `relative` names in `commit`, or None when the commit holds no file there."""
    listed = git.output(["ls-tree", "-z", commit, "--", f":(literal){relative}"], directory=checkout)
    for entry in listed.split("\0"):
        info, _, name = entry.partition("\t")
        fields = info.split()
        if name == relative and len(fields) == 3 and fields[1] == "blob":
            return fields[2]
    return None


def committed_base(relative: str, checkout: Path, git: GitClient, *, text: bool) -> Base:
    """The document as committed at HEAD, a text file's with its line endings as LF, or no base when the checkout has
    no commit yet or HEAD holds no file at `relative`. Any other failure to read either fails with git's reason."""
    try:
        commit = git.commit_of("HEAD", directory=checkout)
    except GitError as exc:
        raise git_failure(exc, "read the checkout's HEAD commit") from exc
    if commit is None:
        if unborn(checkout, git):
            return Base(None, None, "the checkout has no commit yet")
        raise GitError("git could not read the checkout's HEAD commit: HEAD names no commit the repository holds")
    blob_id = committed_blob(commit, relative, checkout, git)
    if blob_id is None:
        return Base(None, None, "the file is not committed")
    try:
        content = git.file_at(commit, relative, directory=checkout)
    except GitError as exc:
        raise git_failure(exc, f"read {relative} as committed at {commit}") from exc
    if content is None:
        raise GitError(f"git could not read {relative} as committed at {commit}: it lists blob {blob_id} there")
    return Base(normalized(content) if text else content, commit, "committed")


def choose_base(requested: str | None, location: Location, git: GitClient, *, text: bool) -> Base:
    if requested == "none":
        return Base(None, None, "the whole document was asked for")
    if location.checkout is None:
        if requested == "committed":
            raise DocumentError("--base committed needs a file inside a git checkout")
        return Base(None, None, "the file is outside any git checkout")
    base = committed_base(location.relative, location.checkout, git, text=text)
    if requested == "committed" and base.content is None:
        raise DocumentError(f"--base committed has nothing to compare with: {base.reason}")
    return base


def repository_name(location: Location) -> str:
    """The fixture's owner/repo: the checkout's folder name, kept to the characters one allows."""
    if location.checkout is None:
        return LOOSE_REPOSITORY
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", location.checkout.name).lstrip("_.-")[:100]
    return f"{LOCAL_OWNER}/{name}" if name else LOOSE_REPOSITORY


def specialists_manifest(relative: str) -> dict[str, Any]:
    """A manifest that routes exactly the document to the suite's design-review specialist. It asks for no
    agent-delegation, so a runtime whose reviewers stay inline reviews it inline."""
    return {
        "schema_version": 2,
        "id": "review-document",
        "protocol_version": 1,
        "kind": "specialists",
        "supports": ["initial", "re-review"],
        "required_capabilities": ["read-diff", "write-result"],
        "resources": [],
        "specialists": [
            {
                "id": "design-review",
                "category": "Design",
                "profile": "suite:design-review",
                "include": [f"^{re.escape(relative)}$"],
                "exclude": [],
                "resources": [],
                "when": None,
            }
        ],
        "conditions": {},
        "finding_categories": [*FINDING_CATEGORIES, "other"],
        "fallback_finding_category": "other",
    }


def same_text(form: str, base: bytes, head: bytes) -> bool:
    """Whether a document's committed version extracts to the lines of text its version on disk does, their labels
    aside, as the diff matches them: an edit that only renumbers the same lines, such as an inserted empty paragraph,
    leaves the diff no hunk to review. A base whose text is not extracted is never the same: the review then reads the
    whole head, and its report says why."""
    try:
        return unlabeled(extract(form, base).decode("utf-8")) == unlabeled(extract(form, head).decode("utf-8"))
    except ExtractionRefused:
        return False


def refuse_path(relative: str, room: PathRoom) -> None:
    """Refuse a path the snapshot's unsafe-path rules refuse in `room`, naming the rule, since the snapshot made from
    the fixture would leave the document out, and a name Windows reads as a drive or a device is never written."""
    reason = unsafe_path_reason(relative, room)
    if reason is not None:
        raise DocumentError(f"{relative} is not a path a fixture can hold: {reason}")


def fixture_target(root: Path, relative: str) -> Path:
    """Where the fixture tree at `root` holds `relative`, refused before anything is written when the snapshot's
    path rules refuse it in the room `root` leaves, or when a reader bound to `root` refuses it: as landing outside
    the fixture when its text leaves `root`, and with the reader's reason when a link on its way leads out of `root`
    or a name on its way cannot be examined, judged with nothing outside `root` followed."""
    refuse_path(relative, path_room(root))
    target = root.joinpath(*PurePosixPath(relative).parts)
    if not within(target, root):
        raise DocumentError(f"{relative} is not a path a fixture can hold: it lands outside the fixture")
    if (refused := TreeReader(root).refusal(target)) is not None:
        raise DocumentError(f"{relative} is not a path a fixture can hold: {refused}")
    return target


def write_file(root: Path, relative: str, content: bytes) -> None:
    target = fixture_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def refuse_unquotable(directory: Path) -> None:
    """Refuse an output directory the PREPARE_ARGUMENTS line cannot name each fixture in as a double-quoted argument
    both shells keep as it is."""
    text = directory.as_posix()
    held = sorted({character for character in text if character in UNQUOTABLE})
    if held or len(text.splitlines()) != 1:
        named = " ".join(held) if held else "a line break"
        raise DocumentError(
            f"{flat_text(text)} holds {named}, which a quoted argument does not keep; name another --output"
        )


def prepare_arguments(fixtures: list[Path]) -> str:
    """The line naming the arguments prepare takes after --host to review every fixture, as the skill passes them."""
    named = " ".join(f'--fixture "{fixture.as_posix()}"' for fixture in fixtures)
    return f"PREPARE_ARGUMENTS --canary {named}"


def output_directory(output: Path | None) -> Path | None:
    """The explicit output directory, checked before anything is read or written, or None for a new temporary one.
    One inside a skills directory is refused, since a file left there makes the deployer skip that skill, and so is
    one that exists and is not empty, so nothing the user keeps there is mixed with the review's files or replaced,
    and one whose path a quoted argument does not keep, or that cannot be looked at, so it is never taken for new."""
    if output is None:
        return None
    given = working_path(output, "review-document-", "")
    # The name as given is judged first: Windows refuses a stat of a name holding `"`, which the walk below would
    # report as a directory it cannot tell about rather than as the name the PREPARE_ARGUMENTS line cannot quote.
    refuse_unquotable(given)
    # Resolving takes a link to nothing on the path to the place it leads to, which the fixture's folders would make.
    try:
        linked = file_presence.link_to_nothing_on(given)
    except OSError as exc:
        raise DocumentError(f"cannot tell whether {given} is a new or empty directory: {exc.strerror or exc}") from exc
    if linked is not None:
        raise DocumentError(f"{given} goes through {linked}, a link to nothing; name a new or empty directory")
    directory = resolved_path(given)
    refuse_unquotable(directory)
    try:
        taken = file_presence.is_occupied(directory) and (
            not file_presence.is_directory(directory, other_is_absent=True) or any(directory.iterdir())
        )
    except OSError as exc:
        raise DocumentError(
            f"cannot tell whether {directory} is a new or empty directory: {exc.strerror or exc}"
        ) from exc
    if taken:
        raise DocumentError(f"{directory} exists and is not an empty directory; name a new or empty directory")
    return directory


def refuse_unchanged(document: Document, change: str, where: str = "") -> None:
    """Refuse a document whose base is its head, or, for a Word document or a PDF, whose base extracts to the same
    lines of text. `change` names the change looked for, and `where` where it was looked for."""
    base, content, form = document.base.content, document.content, document.form
    if base == content:
        raise DocumentError(
            f"{document.relative} has no {change}{where}; pass --base none to review the whole document"
        )
    if form is not None and base is not None and same_text(form, base, content):
        raise DocumentError(
            f"{document.relative} has no {change} to its text{where}, only to formatting, layout, images, or parts "
            "whose text is not extracted; pass --base none to review the whole document"
        )


def reviewed_by_design(relative: str) -> bool:
    """Whether the design-review specialist reviews a document at `relative`, by its suffix ignoring case."""
    return document_format(relative) is not None or PurePosixPath(relative).suffix.lower() in DESIGN_SUFFIXES


def refuse_unreviewable(relative: str) -> None:
    """Refuse a path the pipeline gives no reviewer, by its own rule: one holding a control character or any line
    break Python splits a line at, which would also break the printed DOCUMENT line, a backslash, or an absolute,
    empty, `.`, or `..` segment."""
    if reviewable_path(relative) is None:
        raise DocumentError(f"{relative} is not a path a fixture can hold")


def refuse_reserved(relative: str) -> None:
    """Refuse a path in a folder the review keeps for itself, in any case, since a case-insensitive file system
    merges the spellings: the fixture's manifest folder, and the folder where the snapshot holds a document's
    extracted text, which no repository path may be in."""
    folder = relative.split("/", 1)[0].casefold()
    if folder == MANIFEST_PATH.split("/")[0]:
        raise DocumentError(f"{relative} is where the fixture keeps its reviewer manifest")
    if folder == HELD_TEXT_FOLDER:
        raise DocumentError(f"{relative} is where the source snapshot keeps a document's extracted text")


def refuse_unwritable(fixture: Path, relative: str) -> None:
    """Refuse a document the fixture at `fixture` cannot hold, by its path in the room the fixture's folder leaves
    it and by the reviewer manifest's, before anything of the fixture is written."""
    fixture_target(fixture / "head", relative)
    if reviewed_by_design(relative):
        fixture_target(fixture / "head", MANIFEST_PATH)


def _json_file(value: object) -> bytes:
    """A fixture's JSON file as json_writer writes one, two-space indented with a newline."""
    return (json_writer.dumps(value, indent=2) + "\n").encode("utf-8")


def write_fixture(fixture: Path, document: Document, pull: dict[str, Any], stage: str | None) -> list[str]:
    """Write the one-file fixture of `document` at `fixture`, around the pull request `pull` describes, and return
    the lines that name it. A document the design reviewer reviews is recorded at `stage`, review by default."""
    relative, base = document.relative, document.base
    design = reviewed_by_design(relative)
    refuse_unwritable(fixture, relative)
    pull = {"schema_version": 1, **pull, "threads": []}
    if design:
        pull["manifest_path"] = MANIFEST_PATH
        pull["document_stage"] = stage or DEFAULT_DOCUMENT_STAGE
    # Each JSON file is made before anything of the fixture is written, so a value the JSON writer refuses, such as
    # a path holding a lone surrogate, leaves no fixture behind.
    try:
        manifest = _json_file(specialists_manifest(relative)) if design else None
        pull_file = _json_file(pull)
    except json_writer.JsonWriteRefused as exc:
        raise DocumentError(f"{relative} cannot be held in a fixture: {exc}") from exc
    for tree in ("base", "head"):
        (fixture / tree).mkdir(parents=True)
    if base.content is not None:
        write_file(fixture / "base", relative, base.content)
    write_file(fixture / "head", relative, document.content)
    if manifest is not None:
        for tree in ("base", "head"):
            write_file(fixture / tree, MANIFEST_PATH, manifest)
    write_file(fixture, "pull.json", pull_file)
    return [
        f"DOCUMENT {relative}",
        f"BASE committed {base.commit}" if base.commit else f"BASE none {base.reason}",
        f"ROUTE {'design-review' if design else 'generic'}",
        f"FIXTURE {fixture}",
        f"OUTPUT {fixture.parent}",
    ]


@contextlib.contextmanager
def output_root(directory: Path | None) -> Iterator[Path]:
    """The output directory, or a new temporary one that is removed again when the build fails inside it."""
    if directory is not None:
        yield directory
        return
    root = resolved_path(tempfile.mkdtemp(prefix="review-document-", dir=private_folder.temporary_directory()))
    try:
        refuse_unquotable(root)
        yield root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def refuse_stage(relative: str, stage: str | None) -> None:
    """Refuse a stage for a file the generic reviewer reviews, which has no document stage."""
    if stage is not None and not reviewed_by_design(relative):
        raise DocumentError(
            f"--stage applies to {ROUTED_KINDS}, which the design-review specialist reviews; {relative} goes to the "
            "generic reviewer"
        )


def build(
    path: Path, requested: str | None, output: Path | None, git: GitClient, stage: str | None = None
) -> list[str]:
    directory = output_directory(output)
    path = given_path(path)
    form = document_format(path.name)
    content = document_bytes(path, form)
    location = locate(path, git)
    refuse_unreviewable(location.relative)
    refuse_reserved(location.relative)
    base = choose_base(requested, location, git, text=form is None)
    document = Document(location.relative, content, form, base)
    refuse_unchanged(document, "uncommitted change")
    refuse_stage(location.relative, stage)
    pull = {
        "repository": repository_name(location),
        "number": 1,
        "title": (
            f"Review of {location.relative}: the uncommitted change"
            if base.commit
            else f"Review of {location.relative}: the whole document"
        ),
        "base_ref": f"committed {base.commit}" if base.commit else "none",
        "head_ref": "working tree",
    }
    with output_root(directory) as root:
        return [*write_fixture(root / "fixture", document, pull, stage), prepare_arguments([root / "fixture"])]


def _is_a_path(argument: str) -> bool:
    """Whether anything is at the path `argument` names; a DocumentError when that cannot be told, never read as no
    file there and so as a pull request."""
    try:
        return file_presence.is_occupied(Path(argument))
    except OSError as exc:
        raise DocumentError(f"cannot tell whether {argument} is a file: {exc.strerror or exc}") from exc


def pull_reference(argument: str) -> tuple[str, int] | None:
    """The pull request `argument` names, as owner/repo#number or its URL, or None for a path. A file that exists is
    always a path; any other argument holding a `#`, and any URL, must name a pull request."""
    url = PULL_URL.fullmatch(argument)
    if url is None:
        if argument.casefold().startswith(("http://", "https://")):
            raise DocumentError(f"{argument} is not a pull request URL; name one as {PULL_URL_FORM}")
        if "#" not in argument or _is_a_path(argument):
            return None
    try:
        return parse_pull_selector(f"{url.group(1)}#{url.group(2)}" if url else argument)
    except ValueError as exc:
        raise DocumentError(f"{argument} names no pull request: {exc}") from exc


def pull_changes(github: GitHubClient, pull: Pull, base_tip: str) -> list[Change]:
    """Every file the pull request changes at `pull.head`, as ejryan89-code-review-core's one listing reads them.
    Refused, as the review core refuses them, when the listing is cut short of the files GitHub counts, since a
    document past the cut would go unreviewed, and when the pull request's head is not the one the build began with,
    before the pages or after them, or its base, `base_tip` when the build began, was retargeted by the end of them,
    since the documents are read at that head and that base's merge base and the listing follows both."""
    selector = f"{pull.repository}#{pull.number}"
    listing = github.list_pull_files(pull.repository, pull.number)
    if listing.moved_from(pull.head):
        raise moved(selector, "head", pull.head, listing.head)
    if listing.cut:
        raise DocumentError(
            f"GitHub's files API lists {len(listing.files):,} of the {listing.changed:,} files {selector} changes, "
            "so a document past its cut would go unreviewed; review such a document by its path in a checkout of "
            "the pull request's head"
        )
    now = github.get_pull(pull.repository, pull.number)
    for name, was, key in (("head", pull.head, "headRefOid"), ("base", base_tip, "baseRefOid")):
        if now[key] != was:
            raise moved(selector, name, was, now[key])
    return [Change(file["filename"], file["status"], file["previous_filename"]) for file in listing.files]


def moved(selector: str, name: str, was: str, now: str) -> DocumentError:
    """The refusal of a pull request whose `name`, its head or its base, is now `now`, not the commit `was` the build
    began with."""
    return DocumentError(
        f"{selector} changed while its files were being read ({name} {was[:12]} is now {now[:12]}); run build again"
    )


def pull_blob(github: GitHubClient, repository: str, commit: str, path: str, at: str) -> bytes:
    """The bytes of the file at `path` in `commit`, which `at` describes, once they hash to the blob GitHub names;
    refused over the size a review reads, and for a path GitHub answers as a symbolic link or a submodule."""
    try:
        blob, content = github.get_file_blob(repository, commit, path, maximum=MAX_CHANGED_FILE_BYTES)
    except NotOneFile as exc:
        raise DocumentError(f"{path} is a {exc.entry} {at}, not a file a review reads") from exc
    if content is None:
        raise over_limit(path)
    if not blob_matches(blob, content):
        raise GitHubError(f"GitHub did not return blob {blob} for {path}", kind="malformed")
    return content


def pull_document(github: GitHubClient, pull: Pull, change: Change, requested: str | None, fixture: Path) -> Document:
    """A changed document of the pull request, read at its head and, unless the whole document was asked for, at the
    merge base. Refused, naming the document, when it cannot be reviewed, its path by every rule a fixture's path
    must meet, in the room the folder of its fixture, `fixture`, leaves it, before any of it is read."""
    relative = change.path
    refuse_unreviewable(relative)
    fixture_target(fixture / "head", relative)
    refuse_reserved(relative)
    if change.status == "removed":
        raise DocumentError(f"{relative} is removed in the pull request")
    form = document_format(relative)
    head = pull_blob(github, pull.repository, pull.head, relative, "at the pull request's head")
    content = checked_content(relative, head, form)
    if requested == "none":
        return Document(relative, content, form, Base(None, None, "the whole document was asked for"))
    if change.status == "added":
        return Document(relative, content, form, Base(None, None, "the document is added in the pull request"))
    base = pull_blob(github, pull.repository, pull.base, change.previous or relative, "at the merge base")
    document = Document(relative, content, form, Base(base if form else normalized(base), pull.base, "committed"))
    refuse_unchanged(document, "change", " in the pull request")
    return document


def routed_changes(changes: list[Change], selector: str, wanted: str | None) -> list[Change]:
    """The changes the design reviewer reviews, or the one `wanted` names."""
    routed = [change for change in changes if reviewed_by_design(change.path)]
    if not routed:
        raise DocumentError(
            f"{selector} changes no document review-document reviews: no changed file is {ROUTED_KINDS}"
        )
    if wanted is None:
        return routed
    wanted = wanted.replace("\\", "/")
    chosen = [change for change in routed if change.path == wanted]
    if not chosen:
        named = ", ".join(change.path for change in routed)
        raise DocumentError(f"{wanted} is not a document {selector} changes; name one of: {named}")
    return chosen


def fixture_folder(root: Path, index: int) -> Path:
    """The folder of a pull request's `index`-th fixture, counted from one, in the output directory `root`."""
    return root / str(index) / "fixture"


def refuse_crowded(root: Path, count: int) -> None:
    """Refuse an output directory that leaves the last of `count` fixtures no room for the reviewer manifest every
    fixture of a pull request's documents holds, before any document is read."""
    reason = unsafe_path_reason(MANIFEST_PATH, path_room(fixture_folder(root, count) / "head"))
    if reason is not None:
        raise DocumentError(
            f"{root} leaves a fixture no room for its reviewer manifest, {MANIFEST_PATH}: {reason}; "
            "name a shorter --output"
        )


def pull_documents(
    github: GitHubClient, pull: Pull, routed: list[Change], requested: str | None, wanted: str | None, root: Path
) -> tuple[list[Document], list[str]]:
    """The documents of `routed` that can be reviewed, each judged in the folder of the fixture it is given, and the
    reason each other one is skipped. With `wanted`, a document that cannot be reviewed fails the build, and so does
    a pull request with none left."""
    documents: list[Document] = []
    skipped: list[str] = []
    for change in routed:
        try:
            documents.append(pull_document(github, pull, change, requested, fixture_folder(root, len(documents) + 1)))
        except DocumentError as exc:
            if wanted is not None:
                raise
            skipped.append(flat_text(str(exc)))
    if not documents:
        selector = f"{pull.repository}#{pull.number}"
        raise DocumentError(f"{selector} changes no document that can be reviewed: {'; '.join(skipped)}")
    return documents, skipped


def build_pull(
    reference: tuple[str, int],
    requested: str | None,
    wanted: str | None,
    output: Path | None,
    github: GitHubClient,
    stage: str | None = None,
) -> list[str]:
    """One fixture for each document the pull request changes, or for the one `wanted` names, each in its own
    numbered folder of the output directory and each at `stage`. Without `wanted`, a document that cannot be reviewed
    is skipped, unless none can. Every document is judged, its path in the room its fixture's folder leaves it
    among the rest, before any fixture is written."""
    directory = output_directory(output)
    repository, number = reference
    selector = f"{repository}#{number}"
    metadata = github.get_pull(repository, number)
    head = metadata["headRefOid"]
    pull = Pull(repository, number, github.get_merge_base(repository, metadata["baseRefOid"], head), head)
    routed = routed_changes(pull_changes(github, pull, metadata["baseRefOid"]), selector, wanted)
    if requested == "committed":
        added = next((change.path for change in routed if change.status == "added"), None)
        if added is not None:
            raise DocumentError(f"--base committed has nothing to compare with: {added} is added in the pull request")
    lines = [f"PULL {selector} base {pull.base} head {pull.head}"]
    with output_root(directory) as root:
        refuse_crowded(root, len(routed))
        documents, skipped = pull_documents(github, pull, routed, requested, wanted, root)
        for index, document in enumerate(documents, start=1):
            whole = document.base.commit is None
            lines += write_fixture(
                fixture_folder(root, index),
                document,
                {
                    "repository": repository,
                    "number": number,
                    "title": f"Review of {document.relative} in {selector}: "
                    + ("the whole document" if whole else "its change"),
                    "base_ref": "none" if whole else f"committed {pull.base}",
                    "head_ref": f"head {pull.head}",
                },
                stage,
            )
        fixtures = [fixture_folder(root, index) for index in range(1, len(documents) + 1)]
    return [*lines, *(f"SKIPPED {reason}" for reason in skipped), prepare_arguments(fixtures)]


def json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object in the UTF-8 file at `path`, which the pipeline writes without a byte-order mark, or None when
    it holds none, as a byte-order mark or another encoding does not. A file it cannot read, a directory in its place
    included, is the read's OSError, which names it, never a file that holds no object."""
    try:
        value = json_reader.read_file(path)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def refuse_other_review(fixture: Path, recorded: Path, record: Path) -> None:
    """Refuse a recorded report whose record does not name the review of the fixture's document: its repository and
    the number, title, and refs the fixture's pull.json gives, the title naming the document."""
    pull = json_object(fixture / "pull.json")
    if pull is None:
        raise DocumentError(f"{fixture} is not a fixture review-document built: its pull.json holds no JSON object")
    saved = json_object(record)
    request = saved.get("pull_request") if saved is not None else None
    if saved is None or not isinstance(request, dict):
        raise DocumentError(f"{recorded} is not a report finalize recorded")
    named = {"repository": saved.get("repository"), **{field: request.get(field) for field in REVIEWED_FIELDS}}
    for field, value in named.items():
        if value != pull.get(field):
            raise DocumentError(
                f"{recorded} is not the review of the fixture {fixture}: its record's {field} is {value!r}, the "
                f"fixture's {pull.get(field)!r}"
            )


def _is_file(path: Path, refusal: str) -> bool:
    """Whether a regular file is at `path`: False when nothing is, and the DocumentError `refusal` when something else,
    such as a directory, is, which is never reported as no file."""
    try:
        return file_presence.is_present(path)
    except file_presence.NotAFile as exc:
        raise DocumentError(refusal) from exc


def report(fixture: Path, recorded: Path) -> list[str]:
    """Copy the recorded report beside the fixture, whole or not at all and never over a file already there, once
    its record names the review of the fixture's document, and name the record JSON finalize wrote beside it."""
    fixture = resolved_path(fixture)
    # A file it cannot stat is the stat's OSError, which names it, never one that is not there.
    built = f"{fixture} is not a fixture review-document built"
    if not _is_file(fixture / "pull.json", f"{built}: its pull.json is not a file"):
        raise DocumentError(f"{built}: it holds no pull.json file")
    directory = working_path(fixture.parent, "review-document-", "")
    recorded = resolved_path(recorded)
    record = recorded.with_suffix(".json")
    finalized = f"{recorded} is not a report finalize recorded"
    if recorded.suffix != ".md" or not _is_file(recorded, f"{finalized}: it is not a file"):
        raise DocumentError(finalized)
    if not _is_file(record, f"{finalized}: the {record.name} beside it is not a file"):
        raise DocumentError(f"{finalized}: no {record.name} file is beside it")
    refuse_other_review(fixture, recorded, record)
    target = directory / REPORT_NAME
    content = recorded.read_bytes()
    try:
        # Made whole or not at all, so a copy that fails part way leaves no report.md for the next call to refuse. A
        # file system without hard links has the copy replace an empty report.md reserved under its name, which a
        # reader may meet meanwhile.
        create_bytes(target, content, mode=TRACKED_FILE_MODE, allow_empty_reservation=True)
    except FileExistsError as exc:
        # The copy an earlier call made holds the report's bytes, so a run list reported again names it again.
        if not earlier_copy(target, recorded):
            refused = f"{target} already exists, and is not replaced"
            if is_empty_file(target):
                # No recorded report is empty, so this is the reservation of a copy a kill stopped, or one under way.
                refused += (
                    ": it is empty, as a copy stopped between reserving report.md and filling it leaves it; once no "
                    "report runs, delete it and run report again"
                )
            raise DocumentError(f"{refused}; the report is at {recorded}") from exc
    return [f"REPORT {target}", f"RECORD {record}"]


def earlier_copy(target: Path, recorded: Path) -> bool:
    """Whether the entry at `target` is a copy `report` made of `recorded`: a regular file, never a link, which is
    never followed whatever it leads to, holding the same bytes. One that cannot be read is not shown to be it."""
    if not is_plain_file(target):
        return False
    try:
        return filecmp.cmp(recorded, target, shallow=False)
    except OSError:
        return False


def is_empty_file(target: Path) -> bool:
    """Whether the entry at `target` is an empty regular file, never a link, which is never followed, judged from one
    lstat of it. One that cannot be read, or is gone by then, raises its OSError, which names it."""
    metadata = target.lstat()
    return is_plain_file(target, metadata) and metadata.st_size == 0


# Why a listed run in each status but recorded has no report to copy.
UNRECORDED = {
    "published": "it is not finalized",
    "valid": "it is not finalized",
    "failed": "its review failed",
}


def listed_report(entry: dict[str, str]) -> list[str]:
    """The REPORT and RECORD lines of the run a run list's `entry` names, once its report is copied beside the fixture
    the entry names. Refused, with why, for a run finalize has not recorded, and for an entry naming no fixture or no
    report, as a run of a pull request rather than of a fixture, or one listed before prepare and finalize named them,
    does."""
    status = entry["status"]
    if status in UNRECORDED:
        raise DocumentError(UNRECORDED[status])
    fixture, recorded = entry.get("fixture"), entry.get("report")
    if fixture is None or recorded is None:
        missing = " and ".join(name for name, value in (("fixture", fixture), ("report", recorded)) if value is None)
        raise DocumentError(
            f"the run list names no {missing} for it, as one written before it named them does; run report with "
            "--fixture and --report"
        )
    return report(Path(fixture), Path(recorded))


def report_runs(run_list: Path) -> list[str]:
    """The REPORT and RECORD lines of every run the run list at `run_list` names, each once its report is copied
    beside its fixture, in the order prepare published them, or an UNREPORTED line in place of a run's, with why."""
    runs = load_run_list(run_list)
    if not runs["runs"]:
        raise DocumentError(f"{run_list} lists no prepared run")
    lines: list[str] = []
    for entry in runs["runs"]:
        try:
            lines += listed_report(entry)
        except (DocumentError, PersistenceError, OSError) as exc:
            lines.append(f"UNREPORTED {entry['selector']} {entry['run']} {flat_text(str(exc))}")
    return lines


def refuse_name(name: str) -> None:
    """Refuse a `name` that is not one file name in the working directory a line can print as it is. A colon is
    refused on every platform, since on Windows it makes `D:design.md` a path in drive D's current folder, outside
    the working directory, and `design.md:notes` a stream of `design.md`. With no separator and no colon, a name is
    its own last part as either platform parses it."""
    if (
        not name
        or name in {".", ".."}
        or any(separator in name for separator in "/\\:")
        or LINE_UNSAFE.search(name)
        or name != name.strip()
    ):
        raise DocumentError(
            f"{name!r} is not a file name alone; give the document's path, or a name with no folder, drive, colon, "
            "line break, control character, or space at either end"
        )


def first_folder(folders: tuple[str, ...], matches: Callable[[str], bool]) -> Path | None:
    """The path of the first of `folders`, the names of nested folders, whose name `matches` accepts, or None."""
    for depth, folder in enumerate(folders, start=1):
        if matches(folder):
            return Path(*folders[:depth])
    return None


def unprintable(name: str) -> bool:
    return LINE_UNSAFE.search(name) is not None


def unsearched_lines(walk: Walk, top: Path, wanted: Callable[[str], bool]) -> list[str]:
    """An UNSEARCHED line for each folder or entry the walk could not list or examine, and for the folder its bound
    stopped it at. An entry it could not examine whose name is hidden and not the one searched for is passed over,
    since as a folder the walk would have pruned it, and as a file it is not the document."""
    lines = []
    for skipped in walk.not_entered:
        if skipped.kind not in {UNLISTABLE, UNEXAMINED, BOUNDED}:
            continue
        if skipped.kind == UNEXAMINED and is_hidden(skipped.path) and not wanted(skipped.path.name):
            continue
        relative = skipped.path.relative_to(top)
        lines.append(f"UNSEARCHED {flat_text(str(relative))} {flat_text(skipped.reason)}")
    return lines


def folded(name: str) -> str:
    """`name` as a collision check compares it with no folder to ask (`path_keys.name_key`): composed and case-folded
    fully, so a walk gives every entry a folder may open under it."""
    return name_key(name)


def is_plain(reader: TreeReader, path: Path) -> bool:
    """Whether `path` is a regular file that is no link or reparse point, judged by `reader`: from its own metadata
    first, so a link is never followed, then its status. An `OSError` when either cannot be read, a reader refusal
    included."""
    return not reader.is_link(path) and stat.S_ISREG(reader.status(path).st_mode)


def examined_reason(error: OSError) -> str:
    """Why an entry could not be examined: a reader refusal's reason, or the operating system's."""
    return error.reason if isinstance(error, TreeRefused) else str(error.strerror or error)


def resolve(name: str, top: Path) -> list[str]:
    """The lines naming the files under `top` a program opens as `name`, by the rule the module states, each read
    through a reader bound to `top`."""
    refuse_name(name)
    reader = TreeReader(top)
    try:
        here = is_plain(reader, top / name)
    except OSError:
        here = False
    if here:
        return [f"FOUND {name}"]
    key = folded(name)

    def wanted(entry: str) -> bool:
        return folded(entry) == key

    walk = reader.search(top, wanted, prune=is_hidden, limit=RESOLVE_FOLDER_LIMIT)
    unsearched = unsearched_lines(walk, top, wanted)
    found: list[Path] = []
    for path in walk.files:
        relative = path.relative_to(top)
        try:
            if not reader.opens_as(path, name) or not is_plain(reader, path):
                continue
        except OSError as error:
            reason = flat_text(examined_reason(error))
            unsearched.append(f"UNSEARCHED {flat_text(str(relative))} the file cannot be examined: {reason}")
            continue
        folder = first_folder(relative.parts[:-1], unprintable)
        if folder is None:
            found.append(relative)
            continue
        line = f"UNSEARCHED {flat_text(str(folder))} the folder's name holds a line break or a control character"
        if line not in unsearched:
            unsearched.append(line)
    if not found:
        return [*unsearched, f"NOT_FOUND {name}"]
    if len(found) == 1 and not unsearched:
        return [f"FOUND {found[0]}"]
    return [*unsearched, *(f"CANDIDATE {path}" for path in found)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build", help="write the one-file fixture for each document")
    build_command.add_argument(
        "path", help="the document's path, or a pull request as owner/repo#number or its URL, to review its documents"
    )
    build_command.add_argument("--base", choices=("none", "committed"))
    build_command.add_argument(
        "--stage", choices=tuple(DOCUMENT_STAGES), help=f"the document's stage; {DEFAULT_DOCUMENT_STAGE} by default"
    )
    build_command.add_argument("--document", help="with a pull request, the one changed document to review")
    build_command.add_argument(
        "--output", type=Path, help="the directory for the fixture and the report; a new temporary one by default"
    )
    resolve_command = commands.add_parser("resolve", help="find a document named by its file name alone")
    resolve_command.add_argument("name", help="the document's file name, with no folder")
    report_command = commands.add_parser("report", help="copy each recorded report beside its fixture")
    report_command.add_argument("--runs", type=Path, help="the run list prepare printed; every listed run is reported")
    report_command.add_argument("--fixture", type=Path, help="with --report, the one fixture to report")
    report_command.add_argument("--report", type=Path, help="with --fixture, the report finalize recorded of it")
    return parser


def report_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[str]:
    """Report the runs --runs lists, or the one --fixture and --report name; any other mix is a usage error."""
    one = (args.fixture is None) == (args.report is None) and (args.runs is None) != (args.fixture is None)
    if not one:
        parser.error("report takes --runs, or --fixture and --report together")
    return report_runs(args.runs) if args.runs is not None else report(args.fixture, args.report)


def build_command(args: argparse.Namespace, git: GitClient | None, github: GitHubClient | None) -> list[str]:
    reference = pull_reference(args.path)
    if reference is not None:
        return build_pull(reference, args.base, args.document, args.output, github or GitHubClient(), args.stage)
    if args.document is not None:
        raise DocumentError("--document names a document a pull request changes; name the pull request, not a path")
    return build(Path(args.path), args.base, args.output, git or GitClient(), args.stage)


def main(arguments: list[str] | None = None, git: GitClient | None = None, github: GitHubClient | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "build":
            lines = build_command(args, git, github)
        elif args.command == "resolve":
            lines = resolve(args.name, Path.cwd())
        else:
            lines = report_command(parser, args)
    except (DocumentError, PipelineError, PersistenceError, GitError, GitHubError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(single_line(line) for line in lines))
    return 1 if any(line.startswith(("UNSEARCHED ", "UNREPORTED ")) for line in lines) else 0


if __name__ == "__main__":
    run_main(main)

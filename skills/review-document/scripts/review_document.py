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
  report  Copies the report `finalize` recorded into the output directory, never over a file already there, once its
          record names the review of the fixture's document, and prints REPORT <copy> and RECORD <the record JSON
          beside the original>.
A failure prints `FAILED <reason>` and exits 1; a usage error exits 2.

For a path, the head is the file as it is on disk, refused over the MAX_CHANGED_FILE_BYTES the snapshot reads of a
changed file. The base is the version committed at HEAD when the file lies in a git checkout and is committed there,
under the name git tracks it by, so the review covers the uncommitted change; otherwise, or with `--base none`, the base
has no such file and the whole document is reviewed. A failure git reports reading the commit or the file fails the
build with git's reason.

A pull request is named as owner/repo#number or by its URL, and validated as review-prs validates its --pull
selectors. It is read as review-prs reads one, so a pull request closed without being merged fails, with `FAILED Pull
request is closed without being merged`, and an open or merged one is reviewed. Its changed files are read from
GitHub, through ejryan89-code-review-core's GitHubClient and with no checkout, and those the design reviewer
reviews (DESIGN_SUFFIXES, or a document format) are kept, or the one `--document` names. Each one's head is its
blob at the pull request's head commit, and its base its blob at the merge base GitHub diffs the pull request
against, under its old path when it was renamed; a document the pull request adds has no base, and one GitHub answers
as a symbolic link or a submodule is skipped. The fixture's pull.json names the real pull request, so the record's URL
is its URL, and the commits; the pull request's title, description, branch names, and comments are never read into
it, so the only text from GitHub a reviewer reads is the documents' own. Nothing is written to GitHub.

A Word document or a PDF, a format `review_documents.document_format` names, is written to the fixture as its exact
bytes, so the fixture canary's snapshot extracts its text as it does a pull request's; one whose text would not be
extracted, or whose lines of text the change leaves as they were, labels aside, is refused here first. A Markdown,
plain-text, Word, or PDF document goes to the suite's design-review specialist, through a specialists manifest in the
fixture's base tree, and pull.json records its `--stage`, one of ejryan89-code-review-core's DOCUMENT_STAGES and review
by default, whose fixed sentence prepare puts in that specialist's prompt; any other text file goes to the generic
reviewer, and a stage given for one is refused. The default output is a new temporary directory, so nothing is
written to a checkout unless `--output` names a folder inside one; `--output` must name a new or empty directory, so
nothing already there is mixed with the review's files or replaced.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import json_reader
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError, GitResult, classify_failure
from github_client import GitHubError
from path_containment import within
from review_documents import ExtractionRefused, document_format, extract, unlabeled
from review_github import PULL_PAGE_SIZE, GitHubClient, NotOneFile
from review_github import READ as RESPONSE
from review_io import PersistenceError, working_path
from review_operation import parse_pull_selector
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

# The documents the design-review specialist reviews: Markdown and plain-text markup, matched by suffix, ignoring case.
DESIGN_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})
ROUTED_KINDS = "a Markdown, plain-text, Word, or PDF document"
# A pull request's URL, as GitHub shows it; anything after its number, such as /files, is refused.
PULL_URL = re.compile(r"(?i:https://github\.com)/([^/]+/[^/]+)/pull/([0-9]+)/?")
PULL_URL_FORM = "https://github.com/<owner>/<repo>/pull/<number>"
# Where the fixture keeps its specialists manifest, in both trees, so the manifest is never part of the change.
MANIFEST_PATH = ".review-document/specialists.json"
FINDING_CATEGORIES = ["requirement-gap", "alternative-unstated", "risk", "open-question", "inconsistency", "rollout"]
# The fixture's owner. No GitHub account name holds a dot, so the pull request URL a report shows names nobody's
# repository.
LOCAL_OWNER = "review-document.local"
LOOSE_REPOSITORY = f"{LOCAL_OWNER}/document"
REPORT_NAME = "report.md"
# What a double-quoted argument does not keep as it is in Bash or PowerShell, so an output directory whose path holds
# one cannot be named in the PREPARE_ARGUMENTS line.
UNQUOTABLE = frozenset('"$`\\')
# The fields of a fixture's pull.json a record keeps under its pull_request, which together name the reviewed document.
REVIEWED_FIELDS = ("number", "title", "base_ref", "head_ref")


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


def document_bytes(path: Path, form: str | None) -> bytes:
    """The document's bytes, as `checked_content` keeps them. Refused when it is not a file or is over the size a
    review reads, read no further than one byte past it."""
    if not path.is_file():
        raise DocumentError(f"{path} is not a file")
    with path.open("rb") as source:
        content = source.read(MAX_CHANGED_FILE_BYTES + 1)
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
    """The document's path in its checkout, as git tracks it, or its file name with no checkout."""
    try:
        top = git.output(["rev-parse", "--show-toplevel"], directory=path.parent).strip()
    except GitError as exc:
        if exc.kind == "not_repository":
            return Location(path.name, None)
        raise DocumentError(f"git could not read the checkout around {path}: {exc}") from exc
    checkout = Path(top).resolve()
    relative = PurePosixPath(*path.relative_to(checkout).parts)
    if relative.parts[0] == ".git":
        raise DocumentError(f"{path} is inside the checkout's .git folder")
    return Location(tracked_name(relative.as_posix(), path, checkout, git), checkout)


def same_file(candidate: Path, path: Path) -> bool:
    try:
        return candidate.samefile(path)
    except OSError:
        return False


def tracked_name(relative: str, path: Path, checkout: Path, git: GitClient) -> str:
    """The name git tracks the file at `path` by: `relative` itself, or the one spelling of it, differing only in
    case, that the index holds for the same file, as a file system that ignores case leaves a file renamed only by
    case. `relative` when git tracks neither."""
    listed = git.output(["ls-files", "-z", "--full-name", "--", f":(icase,literal){relative}"], directory=checkout)
    names = [name for name in listed.split("\0") if name]
    if relative in names:
        return relative
    same = [name for name in names if same_file(checkout.joinpath(*PurePosixPath(name).parts), path)]
    if len(same) > 1:
        raise DocumentError(f"{relative} is tracked under several names that differ only in case: {', '.join(same)}")
    return same[0] if same else relative


def git_failure(result: GitResult, doing: str) -> GitError:
    """A git command's failure, classified as GitClient classifies one, with git's reason."""
    reason = result.stderr.strip() or f"exit code {result.returncode}"
    return GitError(
        f"git could not {doing}: {reason}", kind=classify_failure(result.stderr), returncode=result.returncode
    )


def unborn(checkout: Path, git: GitClient) -> bool:
    """Whether HEAD names a branch that has no commit yet, as before a checkout's first commit."""
    branch = git.run(["symbolic-ref", "--quiet", "HEAD"], directory=checkout)
    if branch.returncode != 0:
        return False
    # rev-parse --verify --quiet exits 1, saying nothing, for a ref that does not exist.
    return git.run(["rev-parse", "--verify", "--quiet", branch.stdout.strip()], directory=checkout).returncode == 1


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
    head = git.run(["rev-parse", "--verify", "HEAD^{commit}"], directory=checkout)
    if head.returncode != 0:
        if unborn(checkout, git):
            return Base(None, None, "the checkout has no commit yet")
        raise git_failure(head, "read the checkout's HEAD commit")
    commit = head.stdout.strip()
    blob_id = committed_blob(commit, relative, checkout, git)
    if blob_id is None:
        return Base(None, None, "the file is not committed")
    blob = git.run(["cat-file", "blob", blob_id], directory=checkout)
    if blob.returncode != 0:
        raise git_failure(blob, f"read {relative} as committed at {commit}")
    content = blob.output_bytes()
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
    path rules refuse it in the room `root` leaves, or when it would land outside `root`."""
    refuse_path(relative, path_room(root))
    target = root.joinpath(*PurePosixPath(relative).parts)
    if not within(target.resolve(), root.resolve()):
        raise DocumentError(f"{relative} is not a path a fixture can hold: it lands outside the fixture")
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
    and one whose path a quoted argument does not keep."""
    if output is None:
        return None
    directory = working_path(output, "review-document-", "").resolve()
    refuse_unquotable(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
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


def write_fixture(fixture: Path, document: Document, pull: dict[str, Any], stage: str | None) -> list[str]:
    """Write the one-file fixture of `document` at `fixture`, around the pull request `pull` describes, and return
    the lines that name it. A document the design reviewer reviews is recorded at `stage`, review by default."""
    relative, base = document.relative, document.base
    design = reviewed_by_design(relative)
    refuse_unwritable(fixture, relative)
    for tree in ("base", "head"):
        (fixture / tree).mkdir(parents=True)
    if base.content is not None:
        write_file(fixture / "base", relative, base.content)
    write_file(fixture / "head", relative, document.content)
    pull = {"schema_version": 1, **pull, "threads": []}
    if design:
        manifest = (json.dumps(specialists_manifest(relative), indent=2) + "\n").encode("utf-8")
        for tree in ("base", "head"):
            write_file(fixture / tree, MANIFEST_PATH, manifest)
        pull["manifest_path"] = MANIFEST_PATH
        pull["document_stage"] = stage or DEFAULT_DOCUMENT_STAGE
    (fixture / "pull.json").write_text(json.dumps(pull, indent=2) + "\n", encoding="utf-8")
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
    root = Path(tempfile.mkdtemp(prefix="review-document-")).resolve()
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
    path = path.resolve()
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


def pull_reference(argument: str) -> tuple[str, int] | None:
    """The pull request `argument` names, as owner/repo#number or its URL, or None for a path. A file that exists is
    always a path; any other argument holding a `#`, and any URL, must name a pull request."""
    url = PULL_URL.fullmatch(argument)
    if url is None:
        if argument.casefold().startswith(("http://", "https://")):
            raise DocumentError(f"{argument} is not a pull request URL; name one as {PULL_URL_FORM}")
        if "#" not in argument or Path(argument).exists():
            return None
    try:
        return parse_pull_selector(f"{url.group(1)}#{url.group(2)}" if url else argument)
    except ValueError as exc:
        raise DocumentError(f"{argument} names no pull request: {exc}") from exc


def pull_changes(github: GitHubClient, repository: str, number: int) -> list[Change]:
    """Every file the pull request changes, through every page of its file listing."""
    pages = github.api_json(f"repos/{repository}/pulls/{number}/files?per_page={PULL_PAGE_SIZE}", paginate=True)
    listed = RESPONSE.array(
        pages, "pull file pages", lambda page, path: RESPONSE.array(page, path, RESPONSE.json_object)
    )
    changes = []
    for index, entry in enumerate(file for page in listed for file in page):
        where = f"pull files[{index}]"
        changes.append(
            Change(
                RESPONSE.string(entry.get("filename"), f"{where}.filename"),
                RESPONSE.string(entry.get("status"), f"{where}.status"),
                RESPONSE.optional(RESPONSE.string, entry.get("previous_filename"), f"{where}.previous_filename"),
            )
        )
    return changes


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
    routed = routed_changes(pull_changes(github, repository, number), selector, wanted)
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
    it holds none or cannot be read, as a directory in its place, a byte-order mark, or another encoding cannot."""
    try:
        value = json_reader.read_file(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def refuse_other_review(fixture: Path, recorded: Path, record: Path) -> None:
    """Refuse a recorded report whose record does not name the review of the fixture's document: its repository and
    the number, title, and refs the fixture's pull.json gives, the title naming the document."""
    pull = json_object(fixture / "pull.json")
    if pull is None:
        raise DocumentError(f"{fixture} is not a fixture review-document built")
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


def report(fixture: Path, recorded: Path) -> list[str]:
    """Copy the recorded report beside the fixture, never over a file already there, once its record names the
    review of the fixture's document, and name the record JSON finalize wrote beside it."""
    fixture = fixture.resolve()
    if not (fixture / "pull.json").is_file():
        raise DocumentError(f"{fixture} is not a fixture review-document built")
    directory = working_path(fixture.parent, "review-document-", "")
    recorded = recorded.resolve()
    record = recorded.with_suffix(".json")
    if recorded.suffix != ".md" or not recorded.is_file() or not record.is_file():
        raise DocumentError(f"{recorded} is not a report finalize recorded")
    refuse_other_review(fixture, recorded, record)
    target = directory / REPORT_NAME
    try:
        with recorded.open("rb") as source, target.open("xb") as copy:
            shutil.copyfileobj(source, copy)
    except FileExistsError as exc:
        raise DocumentError(f"{target} already exists, and is not replaced; the report is at {recorded}") from exc
    return [f"REPORT {target}", f"RECORD {record}"]


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
    report_command = commands.add_parser("report", help="copy the recorded report beside the fixture")
    report_command.add_argument("--fixture", type=Path, required=True)
    report_command.add_argument("--report", type=Path, required=True)
    return parser


def build_command(args: argparse.Namespace, git: GitClient | None, github: GitHubClient | None) -> list[str]:
    reference = pull_reference(args.path)
    if reference is not None:
        return build_pull(reference, args.base, args.document, args.output, github or GitHubClient(), args.stage)
    if args.document is not None:
        raise DocumentError("--document names a document a pull request changes; name the pull request, not a path")
    return build(Path(args.path), args.base, args.output, git or GitClient(), args.stage)


def main(arguments: list[str] | None = None, git: GitClient | None = None, github: GitHubClient | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        lines = build_command(args, git, github) if args.command == "build" else report(args.fixture, args.report)
    except (DocumentError, PersistenceError, GitError, GitHubError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main)

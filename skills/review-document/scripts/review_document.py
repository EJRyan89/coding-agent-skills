"""Review one document from its file path, as a one-file fixture canary of code-review-core.

Every command prints one fact per line:
  build   Writes a fixture directory (see review_canary.py in code-review-core) that holds the document as one change,
          and prints DOCUMENT <path in the fixture>, BASE committed <commit> | BASE none <reason>,
          ROUTE design-review | ROUTE generic, FIXTURE <fixture directory>, and OUTPUT <output directory>.
  report  Copies the report `finalize` recorded into the output directory, never over a file already there, and
          prints REPORT <copy> and RECORD <the record JSON beside the original>.
A failure prints `FAILED <reason>` and exits 1; a usage error exits 2.

The head is the file as it is on disk. The base is the version committed at HEAD when the file lies in a git checkout
and is committed there, so the review covers the uncommitted change; otherwise, or with `--base none`, the base has no
such file and the whole document is reviewed. A Word document or a PDF, a format `review_documents.document_format`
names, is written to the fixture as its exact bytes, so the fixture canary's snapshot extracts its text as it does a
pull request's; one whose text would not be extracted, or whose lines of text the change leaves as they were, labels
aside, is refused here first. A Markdown, plain-text, Word, or PDF document (DESIGN_SUFFIXES, or a document format)
goes to the suite's design-review specialist, through a specialists manifest in the fixture's base tree; any other
text file goes to the generic reviewer. Nothing here calls GitHub, nothing is written to the checkout, and `--output`
must name a new or empty directory, so nothing already there is mixed with the review's files or replaced.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from review_documents import ExtractionRefused, document_format, extract, unlabeled
from review_io import PersistenceError, working_path

# The documents the design-review specialist reviews: Markdown and plain-text markup, matched by suffix, ignoring case.
DESIGN_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})
# Where the fixture keeps its specialists manifest, in both trees, so the manifest is never part of the change.
MANIFEST_PATH = ".review-document/specialists.json"
FINDING_CATEGORIES = ["requirement-gap", "alternative-unstated", "risk", "open-question", "inconsistency", "rollout"]
# git's own test for a binary file: a NUL byte in the first 8,000 bytes.
BINARY_PROBE = 8000
# The fixture's owner. No GitHub account name holds a dot, so the pull request URL a report shows names nobody's
# repository.
LOCAL_OWNER = "review-document.local"
LOOSE_REPOSITORY = f"{LOCAL_OWNER}/document"
REPORT_NAME = "report.md"


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


def document_bytes(path: Path, form: str | None) -> bytes:
    """The document's bytes: a text file's with its line endings as LF, or a Word document's or a PDF's exactly, once
    its text extracts to something to review. Refused when it is empty, binary, or a document whose text is not
    extracted."""
    if not path.is_file():
        raise DocumentError(f"{path} is not a file")
    content = path.read_bytes()
    if not content.strip():
        raise DocumentError(f"{path} is empty")
    if form is not None:
        if not document_text(path, form, content).strip():
            raise DocumentError(f"{path} holds no text to review")
        return content
    if b"\0" in content[:BINARY_PROBE]:
        raise DocumentError(f"{path} is not a text file, a Word document, or a PDF")
    return normalized(content)


def document_text(path: Path, form: str, content: bytes) -> bytes:
    """A document's text as the snapshot extracts it, refused for the reason the snapshot would give."""
    try:
        return extract(form, content)
    except ExtractionRefused as exc:
        raise DocumentError(f"{path} is a document whose text could not be extracted: {exc}") from exc


def normalized(content: bytes) -> bytes:
    """Line endings as LF, so a checkout that converts them on checkout never makes every line a change."""
    return content.replace(b"\r\n", b"\n")


def locate(path: Path, git: GitClient) -> Location:
    """The document's path in its checkout, or its file name with no checkout."""
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
    return Location(relative.as_posix(), checkout)


def committed_base(relative: str, checkout: Path, git: GitClient, *, text: bool) -> Base:
    """The document as committed at HEAD, a text file's with its line endings as LF, or no base when it has never
    been committed."""
    head = git.run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], directory=checkout)
    if head.returncode != 0:
        return Base(None, None, "the checkout has no commit yet")
    commit = head.stdout.strip()
    blob = git.run(["cat-file", "blob", f"{commit}:{relative}"], directory=checkout)
    if blob.returncode != 0:
        return Base(None, None, "the file is not committed")
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
    agent-delegation, so a runtime that cannot start subagents reviews it inline."""
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


def write_file(root: Path, relative: str, content: bytes) -> None:
    target = root.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def output_directory(output: Path | None) -> Path | None:
    """The explicit output directory, checked before anything is read or written, or None for a new temporary one.
    One inside a skills directory is refused, since a file left there makes the deployer skip that skill, and so is
    one that exists and is not empty, so nothing the user keeps there is mixed with the review's files or replaced."""
    if output is None:
        return None
    directory = working_path(output, "review-document-", "").resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise DocumentError(f"{directory} exists and is not an empty directory; name a new or empty directory")
    return directory


def build(path: Path, requested: str | None, output: Path | None, git: GitClient) -> list[str]:
    directory = output_directory(output)
    path = path.resolve()
    form = document_format(path.name)
    content = document_bytes(path, form)
    location = locate(path, git)
    if location.relative.startswith(MANIFEST_PATH.split("/")[0] + "/"):
        raise DocumentError(f"{location.relative} is where the fixture keeps its reviewer manifest")
    base = choose_base(requested, location, git, text=form is None)
    if base.content == content:
        raise DocumentError(
            f"{location.relative} has no uncommitted change; pass --base none to review the whole document"
        )
    if form is not None and base.content is not None and same_text(form, base.content, content):
        raise DocumentError(
            f"{location.relative} has no uncommitted change to its text, only to formatting, layout, images, or parts "
            "whose text is not extracted; pass --base none to review the whole document"
        )
    design = form is not None or path.suffix.lower() in DESIGN_SUFFIXES
    fixture = (directory or Path(tempfile.mkdtemp(prefix="review-document-")).resolve()) / "fixture"
    for tree in ("base", "head"):
        (fixture / tree).mkdir(parents=True)
    if base.content is not None:
        write_file(fixture / "base", location.relative, base.content)
    write_file(fixture / "head", location.relative, content)
    pull: dict[str, Any] = {
        "schema_version": 1,
        "repository": repository_name(location),
        "number": 1,
        "title": (
            f"Review of {location.relative}: the uncommitted change"
            if base.commit
            else f"Review of {location.relative}: the whole document"
        ),
        "base_ref": f"committed {base.commit}" if base.commit else "none",
        "head_ref": "working tree",
        "threads": [],
    }
    if design:
        manifest = (json.dumps(specialists_manifest(location.relative), indent=2) + "\n").encode("utf-8")
        for tree in ("base", "head"):
            write_file(fixture / tree, MANIFEST_PATH, manifest)
        pull["manifest_path"] = MANIFEST_PATH
    (fixture / "pull.json").write_text(json.dumps(pull, indent=2) + "\n", encoding="utf-8")
    return [
        f"DOCUMENT {location.relative}",
        f"BASE committed {base.commit}" if base.commit else f"BASE none {base.reason}",
        f"ROUTE {'design-review' if design else 'generic'}",
        f"FIXTURE {fixture}",
        f"OUTPUT {fixture.parent}",
    ]


def report(fixture: Path, recorded: Path) -> list[str]:
    """Copy the recorded report beside the fixture, never over a file already there, and name the record JSON
    finalize wrote beside it."""
    fixture = fixture.resolve()
    if not (fixture / "pull.json").is_file():
        raise DocumentError(f"{fixture} is not a fixture review-document built")
    directory = working_path(fixture.parent, "review-document-", "")
    recorded = recorded.resolve()
    record = recorded.with_suffix(".json")
    if recorded.suffix != ".md" or not recorded.is_file() or not record.is_file():
        raise DocumentError(f"{recorded} is not a report finalize recorded")
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
    build_command = commands.add_parser("build", help="write the one-file fixture for a document")
    build_command.add_argument("path", type=Path)
    build_command.add_argument("--base", choices=("none", "committed"))
    build_command.add_argument(
        "--output", type=Path, help="the directory for the fixture and the report; a new temporary one by default"
    )
    report_command = commands.add_parser("report", help="copy the recorded report beside the fixture")
    report_command.add_argument("--fixture", type=Path, required=True)
    report_command.add_argument("--report", type=Path, required=True)
    return parser


def main(arguments: list[str] | None = None, git: GitClient | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        if args.command == "build":
            lines = build(args.path, args.base, args.output, git or GitClient())
        else:
            lines = report(args.fixture, args.report)
    except (DocumentError, PersistenceError, GitError, OSError) as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main)

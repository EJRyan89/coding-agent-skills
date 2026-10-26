"""A run's diff and source snapshot: fetching the head, the snapshot, and changed documents."""

from __future__ import annotations

import functools
import json
import re
import shutil
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from diff_paths import header_text, quote
from git_client import GitClient, GitError, Runner
from github_client import GitHubError, replace_undecodable
from path_keys import path_key
from review_analyzers import reads_settings
from review_documents import EXTRACTED_WHEN_TEXT, ExtractionRefused, document_diff, document_format, extract, locations
from review_github import DiffOverLimit
from review_io import atomic_write_text, read_diff
from review_operation import validate_canary_pull
from review_records import DIFF_SOURCES, DiffFallback
from review_run import PipelineError, Services
from review_runtime import (
    GIT_DIFF_OPTIONS,
    MAX_CHANGED_FILE_BYTES,
    MAX_SOURCE_SNAPSHOT_BYTES,
    SNAPSHOT_EXTRACTED,
    SNAPSHOT_FETCHABLE,
    RuntimeContractError,
    blob_matches,
    git_in,
    glob_matcher,
    held_path,
    materialize_source_snapshot,
    materialize_source_snapshot_from_github,
    read_blob,
    snapshot_bytes,
    verify_checkout_remote,
)
from review_specialists import (
    SpecialistError,
    condition_reads,
    diff_block_sides,
    load_materialized_manifest,
    parse_unified_diff,
    patch_fingerprints,
    split_unified_diff,
    symbolic_links,
)
from review_vocabularies import DispatchMode

GIT_STATUS_ALLOWED = {
    "ensure_local_commit": (
        "reports git's reason to the user: a failed fetch raises a PipelineError naming git's stderr"
    ),
}


def _has_commit(checkout: Path, commit: str, git: Runner) -> bool:
    """Whether the checkout holds `commit`. Only git's answer that it names no commit is no; any other failure, such
    as a repository git cannot read, raises, so it is never taken for a commit to fetch."""
    try:
        return GitClient(git).commit_of(commit, directory=checkout) is not None
    except GitError as exc:
        raise PipelineError(f"git cannot tell whether {checkout} holds commit {commit}: {exc}") from exc


_FETCH_LOCKS: dict[str, threading.Lock] = {}


_FETCH_LOCKS_GUARD = threading.Lock()


def _fetch_lock(checkout: Path) -> threading.Lock:
    """One lock per checkout: concurrent fetches into one repository contend for FETCH_HEAD and ref locks."""
    key = path_key(checkout, resolve=True)
    with _FETCH_LOCKS_GUARD:
        return _FETCH_LOCKS.setdefault(key, threading.Lock())


def ensure_local_commit(checkout: Path, commit: str, refspec: str, git: Runner) -> None:
    """Use the commit from the local clone when it is already there; fetch only what is missing."""
    if _has_commit(checkout, commit, git):
        return
    with _fetch_lock(checkout):
        if _has_commit(checkout, commit, git):  # another pull request's fetch may have brought it
            return
        result = git_in(checkout, git, "fetch", "--no-tags", "--quiet", "origin", refspec)
    if result.returncode != 0:
        raise PipelineError(f"Cannot fetch {refspec}: {result.stderr.strip() or 'git fetch failed'}")
    if not _has_commit(checkout, commit, git):
        raise PipelineError(f"Commit {commit} is not available after fetching {refspec}")


# The most diff prepare takes from the checkout or from GitHub's files API, which, unlike GitHub's diff endpoint, bound
# no diff themselves: the most a snapshot writes, since the diff is written into the same run.
MAX_DIFF_BYTES = MAX_SOURCE_SNAPSHOT_BYTES
# The blob of an empty file, in a SHA-1 and a SHA-256 repository: an empty file's entry has no patch either.
EMPTY_BLOBS = frozenset(
    {
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",
    }
)


class PullDiff(NamedTuple):
    """A pull request's diff, how many undecodable bytes in it became U+FFFD, where it came from when GitHub's diff
    endpoint refused it, and the changed files whose patch GitHub's files API left out."""

    text: str
    undecodable: int
    fallback: DiffFallback | None = None
    omitted: tuple[str, ...] = ()


class ChangeRead(NamedTuple):
    """What prepare reads of a pull request's change: the parsed diff, its changed paths, the changed paths whose
    change no reviewer can be given whole (each noted), their patch fingerprints, the open review comments, and where
    the diff came from when GitHub's diff endpoint refused it."""

    parsed: dict[str, dict[str, Any]]
    changed: list[str]
    gaps: list[str]
    patches: dict[str, dict[str, Any]]
    comments: list[dict[str, Any]]
    fallback: DiffFallback | None


def _moved(selector: str, head: str, now: str) -> PipelineError:
    return PipelineError(
        f"{selector} changed while it was being prepared (head {head[:12]} is now {now[:12]}); run prepare again"
    )


def read_pull_diff(
    repository: str, number: int, pull: dict[str, Any], checkout: Path | None, services: Services
) -> PullDiff:
    """The pull request's diff from GitHub's diff endpoint, or, when the endpoint refuses it over one of its limits,
    from git in the configured checkout, where the merge base and the head are local, or else from GitHub's files
    API, every page of it. Neither is bounded by GitHub, so either fails past MAX_DIFF_BYTES, and the files API fails
    for a change it does not list whole, naming the limit and the pull request."""
    try:
        return PullDiff(*services.github.get_pull_diff(repository, number))
    except DiffOverLimit as refused:
        limit = refused.limit
    selector = f"{repository}#{number}"
    over = f"{selector} is over GitHub's diff limit of {limit}"
    if checkout is not None:
        text, undecodable = _checkout_diff(checkout, repository, number, pull, services, over)
        return PullDiff(text, undecodable, {"source": "checkout", "limit": limit})
    listing = services.github.list_pull_files(repository, number)
    if listing.head != pull["headRefOid"]:
        raise _moved(selector, pull["headRefOid"], listing.head)
    if len(listing.files) != listing.changed:
        raise PipelineError(
            f"{over}, and GitHub's files API lists {len(listing.files):,} of its {listing.changed:,} changed files; "
            f"set checkout_path for {repository} so prepare reads its diff from the checkout"
        )
    text, omitted = files_diff(listing.files)
    if len(text.encode("utf-8", "surrogateescape")) > MAX_DIFF_BYTES:
        raise PipelineError(f"{over}, and its diff from GitHub's files API is over {MAX_DIFF_BYTES:,} bytes")
    return PullDiff(text, 0, {"source": "files-api", "limit": limit}, omitted)


def _checkout_diff(
    checkout: Path, repository: str, number: int, pull: dict[str, Any], services: Services, over: str
) -> tuple[str, int]:
    """The pull request's diff as git computes it in the checkout from the merge base GitHub diffs against to the
    head, fetching the head when it is not local, read through a pipe and refused past MAX_DIFF_BYTES."""
    head = pull["headRefOid"]
    verify_checkout_remote(checkout, repository, services.git)
    ensure_local_commit(checkout, head, f"refs/pull/{number}/head", services.git)
    # The merge base is an ancestor of the head, so it is local once the head is.
    base = services.github.get_merge_base(repository, pull["baseRefOid"], head)
    if not _has_commit(checkout, base, services.git):
        raise PipelineError(f"{over}, and its merge base {base[:12]} is not in {checkout}")
    try:
        with GitClient().stream(["diff", *GIT_DIFF_OPTIONS, base, head], directory=checkout) as stream:
            stream.stdin.close()
            raw = stream.read(MAX_DIFF_BYTES + 1)
            if len(raw) > MAX_DIFF_BYTES:
                raise PipelineError(f"{over}, and its diff in {checkout} is over {MAX_DIFF_BYTES:,} bytes")
            status = stream.wait()
            if status != 0:
                raise PipelineError(
                    f"{over}, and git diff failed in {checkout}: {stream.stderr().strip() or f'exit code {status}'}"
                )
    except GitError as exc:
        raise PipelineError(f"{over}, and git diff failed in {checkout}: {exc}") from exc
    return replace_undecodable(raw.decode("utf-8", "surrogateescape"))


def files_diff(files: Sequence[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    """A unified diff of the files GitHub's files API lists, as git writes one, and the files that change lines but
    whose patch GitHub left out, as it does for a diff too large to show.

    The listing names no file mode, so an added or a deleted file's block names 100644, and no base blob, so a
    block names none. A file with no patch and no changed line is binary, an empty file, a rename, or a mode change:
    its block is git's header alone, with git's binary line for an added, deleted, or modified file that is not empty.
    """
    blocks: list[str] = []
    omitted: list[str] = []
    for entry in files:
        new, status, patch = entry["filename"], entry["status"], entry["patch"]
        old = entry["previous_filename"] or new
        before = None if status == "added" else f"a/{old}"
        after = None if status == "removed" else f"b/{new}"
        lines = [f"diff --git {quote(f'a/{old}')} {quote(f'b/{new}')}"]
        if status in ("added", "removed"):
            lines.append(f"{'new' if status == 'added' else 'deleted'} file mode 100644")
        elif status in ("renamed", "copied"):
            verb = "rename" if status == "renamed" else "copy"
            lines.extend([f"{verb} from {quote(old)}", f"{verb} to {quote(new)}"])
        if patch:
            lines.extend(
                [f"--- {header_text(before)}", f"+++ {header_text(after)}", *patch.removesuffix("\n").split("\n")]
            )
        elif entry["changes"]:
            omitted.append(new)
        elif status in ("added", "removed", "modified") and entry["sha"] not in EMPTY_BLOBS:
            lines.append(f"Binary files {_binary_side(before)} and {_binary_side(after)} differ")
        blocks.append("\n".join(lines) + "\n")
    return "".join(blocks), tuple(omitted)


def describe_fallback(fallback: DiffFallback) -> str:
    """The note that names where a diff GitHub's diff endpoint refused came from instead, and the limit."""
    return (
        f"GitHub's diff endpoint refuses a diff over {fallback['limit']}, so the diff was read from "
        f"{DIFF_SOURCES[fallback['source']]}."
    )


def _binary_side(value: str | None) -> str:
    return "/dev/null" if value is None else quote(value)


def _fetch_diff(
    repository: str,
    number: int,
    pull: dict[str, Any],
    checkout: Path | None,
    diff_path: Path,
    services: Services,
    notes: list[str],
) -> ChangeRead:
    """Write the pull request's diff to `diff_path` once the pull is confirmed unmoved, as `read_pull_diff` reads it.
    Returns what prepare reads of the change."""
    selector = f"{repository}#{number}"
    head = pull["headRefOid"]
    diff = read_pull_diff(repository, number, pull, checkout, services)
    # GitHub serves the diff and the files by pull number, which follows pushes. Confirm the pull did not move after
    # its head was read, so a new diff is never archived under the old head SHA.
    current = validate_canary_pull(services.github.get_pull(repository, number), repository=repository, number=number)
    if (current["headRefOid"], current["baseRefOid"]) != (head, pull["baseRefOid"]):
        raise _moved(selector, head, current["headRefOid"])
    parsed, changed, gaps, patches = _write_diff(selector, diff, diff_path, notes)
    comments = services.github.list_open_review_threads(repository, number)
    return ChangeRead(parsed, changed, gaps, patches, comments, diff.fallback)


def _write_diff(
    selector: str, diff: PullDiff, diff_path: Path, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]]]:
    """Write a pull request's diff to `diff_path`. Returns the parsed diff, its changed paths, the changed paths whose
    change no reviewer can be given whole, each noted: those whose path a reviewer cannot be given, and those whose
    patch GitHub's files API left out; and their patch fingerprints."""
    atomic_write_text(diff_path, diff.text)
    if diff.fallback is not None:
        notes.append(describe_fallback(diff.fallback))
    if diff.undecodable:
        notes.append(f"{diff.undecodable} undecodable bytes replaced in the diff")
    parsed, unsafe = split_unified_diff(diff.text)
    changed = list(parsed)
    patches = patch_fingerprints(parsed)
    # Escaped, because each note is a line the orchestrator reads, and a raw path could add a line of its own.
    named = ", ".join(json.dumps(path) for path in unsafe)
    if not changed:
        raise PipelineError(
            f"{selector} changes no files" + (f" a reviewer can be given safely: {named}" if unsafe else "")
        )
    if unsafe:
        notes.append(
            f"Not reviewed: {len(unsafe)} changed file{'s' if len(unsafe) != 1 else ''} whose path a reviewer prompt "
            f"cannot carry safely (a control character, a backslash, or an absolute, empty, '.', or '..' segment), "
            f"recorded as unavailable sources: {named}."
        )
    omitted = [path for path in diff.omitted if path in parsed]
    if omitted:
        notes.append(
            f"Not reviewed in full: {len(omitted)} changed file{'s' if len(omitted) != 1 else ''} whose patch GitHub's "
            "files API leaves out, as it does for a diff too large to show, recorded as unavailable sources: "
            + ", ".join(json.dumps(path) for path in omitted)
            + "."
        )
    return parsed, changed, [*unsafe, *omitted], patches


def _snapshot(
    checkout: Path | None,
    repository: str,
    number: int,
    head: str,
    source: Path,
    parsed: dict[str, dict[str, Any]],
    changed: list[str],
    services: Services,
    notes: list[str],
    exclude: Sequence[str] = (),
    unreviewed_suffixes: Sequence[str] = (),
    fallback: DiffFallback | None = None,
) -> tuple[dict[str, tuple[int, str] | None], dict[str, Any], dict[str, Any]]:
    """Snapshot the head at `source`, from the checkout or else GitHub's tarball, leaving out what the repository's
    `exclude` globs match, probing a changed file over the size limit by the shipped suffixes and the repository's
    `unreviewed_suffixes`, and noting each changed document whose text it could not extract. Returns the symbolic
    links the snapshot leaves out that the pull request changes, each one noted, the snapshot's statistics: its
    source, files, bytes, and exclusions by reason, where the diff came from when GitHub's diff endpoint refused it
    (`fallback`), and the seconds spent fetching the head and materializing it, and
    the manifest materializing it verified, which the request and the plan take in place of verifying the snapshot
    again.

    A checkout's snapshot is lazy: it holds the changed files and the analyzer settings, and its reviewers fetch the
    rest through review_source.py. `_retake_snapshot` makes it whole where they cannot, and adds the paths the
    run's condition scripts declare they read."""
    fetched = 0.0
    refused: dict[str, str] = {}
    started = services.timer()
    if checkout is not None:
        verify_checkout_remote(checkout, repository, services.git)
        ensure_local_commit(checkout, head, f"refs/pull/{number}/head", services.git)
        fetched = services.timer() - started
        snapshot = materialize_source_snapshot(
            checkout,
            repository,
            head,
            source,
            runner=services.git,
            changed_paths=changed,
            upfront=lazy_upfront(()),
            exclude=exclude,
            refused=refused,
            unreviewed_suffixes=unreviewed_suffixes,
        )
    else:

        def fetcher(repository: str, commit: str, target: Path) -> None:
            nonlocal fetched
            began = services.timer()
            services.fetch_tarball(repository, commit, target)
            fetched += services.timer() - began

        snapshot = materialize_source_snapshot_from_github(
            repository,
            head,
            source,
            fetcher=fetcher,
            changed_paths=changed,
            exclude=exclude,
            refused=refused,
            unreviewed_suffixes=unreviewed_suffixes,
        )
    materialized = services.timer() - started - fetched
    # Only the links this pull request adds or changes: a repository that keeps links is not told on every review.
    links = symbolic_links(parsed, snapshot["excluded_paths"])
    notes.extend(f"snapshot excludes symbolic link {path}" for path in links)
    # A snapshot taken again for the same change refuses the same documents, so these notes stand for it too.
    notes.extend(
        f"Not reviewed in full: the text of document {json.dumps(path)} could not be extracted ({reason}), so it "
        "is recorded as an unavailable source."
        for path, reason in sorted(refused.items())
    )
    stats = {
        "source": "tarball" if checkout is None else "checkout-lazy",
        "files": len(snapshot["source_hashes"]),
        "bytes": snapshot_bytes(source, snapshot),
        "excluded": exclusion_counts(snapshot["excluded_paths"]),
        "seconds": {"fetch": fetched, "materialize": materialized},
        **({} if fallback is None else {"diff": dict(fallback)}),
    }
    return links, stats, snapshot


def exclusion_counts(excluded: dict[str, str]) -> dict[str, int]:
    """How many paths a snapshot leaves out for each reason, by reason."""
    counts = Counter(excluded.values())
    return {reason: counts[reason] for reason in sorted(counts)}


def lazy_upfront(reads: Sequence[str]) -> Callable[[str], bool]:
    """The paths a lazy snapshot holds from the start beside the changed files: the analyzer settings, and those the
    `reads` glob patterns of its condition scripts match."""
    matchers = [glob_matcher(pattern) for pattern in reads]
    return lambda path: reads_settings(path) or any(matches(path) for matches in matchers)


def _snapshot_reads(
    snapshot: dict[str, Any],
    dispatch: DispatchMode,
    kind: str,
    reviewer_root: Path | None,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
) -> list[str] | None:
    """The glob patterns of the snapshot paths a run's condition scripts read, which its lazy snapshot must hold from
    the start; empty when it runs none or the snapshot is already whole. None when its reviewers need every file from
    the start: the Copilot CLI host runs no command, so it cannot fetch one, and a condition that declares no `reads`
    reads the snapshot as it chooses."""
    if SNAPSHOT_FETCHABLE not in snapshot:
        return []
    manifest = None
    if kind == "specialists" and reviewer_root is not None:
        try:
            manifest = load_materialized_manifest(reviewer_root)[0]
        except SpecialistError as exc:
            raise PipelineError(str(exc)) from exc
    return _route_reads(dispatch, manifest, changed, renamed)


def _route_reads(
    dispatch: DispatchMode | None,
    manifest: dict[str, Any] | None,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
) -> list[str] | None:
    """The route a checkout's snapshot takes for a run dispatched as `dispatch` (None when it is not the Copilot CLI
    host) whose reviewer's `manifest` (None for the generic reviewer) sees these changed files, with the earlier paths
    `renamed` gives them (see `review_specialists.renamed_from`): the glob patterns of
    the paths its lazy snapshot holds from the start, or None when it is written whole, as `_snapshot_reads` says.
    prepare and validate-reviewer both decide the route here, so they measure the same snapshot."""
    if dispatch == "copilot-host":
        return None
    if manifest is None or manifest.get("kind") != "specialists":
        return []
    return condition_reads(manifest, changed, renamed)


def _retake_snapshot(
    checkout: Path,
    repository: str,
    head: str,
    source: Path,
    changed: list[str],
    services: Services,
    stats: dict[str, Any],
    exclude: Sequence[str] = (),
    reads: Sequence[str] | None = None,
    unreviewed_suffixes: Sequence[str] = (),
) -> dict[str, Any]:
    """Replace a lazy snapshot, adding the time it takes to `stats`: with the whole one, now of a `checkout` snapshot,
    when `reads` is None, or else with a lazy one that also holds the paths its glob patterns match, probing as
    `_snapshot` does. Returns the manifest materializing it verified."""
    started = services.timer()
    shutil.rmtree(source)
    snapshot = materialize_source_snapshot(
        checkout,
        repository,
        head,
        source,
        runner=services.git,
        changed_paths=changed,
        upfront=None if reads is None else lazy_upfront(reads),
        exclude=exclude,
        unreviewed_suffixes=unreviewed_suffixes,
    )
    stats["source"] = "checkout" if reads is None else "checkout-lazy"
    stats["files"] = len(snapshot["source_hashes"])
    stats["bytes"] = snapshot_bytes(source, snapshot)
    stats["excluded"] = exclusion_counts(snapshot["excluded_paths"])
    stats["seconds"]["materialize"] += services.timer() - started
    return snapshot


# Reads a changed document's base version: its bytes, or None for one over the changed-file limit.
BaseReader = Callable[[str, str], bytes | None]


DIFF_BLOCK = re.compile(r"^diff --git ", re.MULTILINE)


INDEX_LINE = re.compile(r"index ([0-9a-f]+)\.\.([0-9a-f]+)")


def _base_reader(checkout: Path | None, repository: str, pull: dict[str, Any], services: Services) -> BaseReader:
    """How a review reads a changed document's base version, by the old path and abbreviated blob id its diff names:
    from the checkout's object store, or else from GitHub at the merge base the diff was taken against, where the
    blob GitHub names must start with that id and hash to its own."""
    if checkout is not None:

        def from_checkout(_path: str, abbreviation: str) -> bytes | None:
            return read_blob(checkout, abbreviation, services.git, maximum=MAX_CHANGED_FILE_BYTES)

        return from_checkout
    merge_base = functools.cache(
        lambda: services.github.get_merge_base(repository, pull["baseRefOid"], pull["headRefOid"])
    )

    def from_github(path: str, abbreviation: str) -> bytes | None:
        blob, content = services.github.get_file_blob(repository, merge_base(), path, maximum=MAX_CHANGED_FILE_BYTES)
        if not blob.startswith(abbreviation) or (content is not None and not blob_matches(blob, content)):
            raise RuntimeContractError(f"GitHub did not return blob {abbreviation}")
        return content

    return from_github


def _document_block(block: str, form: str) -> tuple[list[str], str | None] | None:
    """A changed document's diff block's header lines and the abbreviated base blob id its index line names (None for
    an added file), or None for a block that is not one file's binary block, or, for a format extracted whether or not
    it is binary, one file's text block."""
    header: list[str] = []
    old: str | None = None
    for raw in block.rstrip("\n").split("\n"):
        line = raw.removesuffix("\r")
        if header and line.startswith("diff --git "):
            return None
        if line.startswith(("Binary files ", "GIT binary patch")):
            return header, old
        if line.startswith("--- ") and form in EXTRACTED_WHEN_TEXT:
            return header, old
        if line.startswith(("@@", "--- ", "+++ ")):
            return None
        index = INDEX_LINE.match(line)
        if index is not None:
            old = index.group(1) if index.group(1).strip("0") else None
        header.append(line)
    return None


def _extracted_base(form: str, old: str | None, blob: str | None, read_base: BaseReader) -> tuple[str, str | None]:
    """A changed document's base version as extracted text, and why it could not be extracted, if it could not."""
    if old is None:
        return "", None
    if blob is None:
        return "", "its diff names no base blob"
    try:
        content = read_base(old, blob)
        if content is None:
            return "", "it is over the changed-file size limit"
        return extract(form, content).decode("utf-8"), None
    except ExtractionRefused as exc:
        return "", str(exc)
    except (GitHubError, RuntimeContractError):
        return "", "it could not be read"


def _document_diffs(
    diff_path: Path,
    parsed: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    source: Path,
    read_base: BaseReader,
    notes: list[str],
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]] | None:
    """Give each changed document the snapshot holds as extracted text, or the pull request deletes, a diff of its
    base and head text in place of its binary block in `diff_path`. Returns, for each document the snapshot holds,
    each line's place in the document, which a finding on that line records, and the patch fingerprints of the diff
    as rewritten; None when it changes nothing.

    The head text is the snapshot's own file, so a hunk's line numbers are that file's and a finding anchors to it.
    """
    extracted = snapshot.get(SNAPSHOT_EXTRACTED, {})
    rendered: dict[str, str] = {}
    places: dict[str, list[str]] = {}
    for path, entry in parsed.items():
        form = document_format(path)
        binary = None if form is None else _document_block(entry["block"], form)
        if form is None or binary is None:
            continue
        old, new = diff_block_sides(entry["block"])
        held = held_path(path, form)
        if path in extracted:
            head = source.joinpath(*Path(held).parts).read_text(encoding="utf-8")
            places[path] = locations(head)
        elif new is None:
            head = ""
        else:
            continue
        base, refused = _extracted_base(form, old, binary[1], read_base)
        if refused is not None:
            notes.append(f"The base version of document {json.dumps(path)} could not be extracted ({refused}).")
        rendered[path] = document_diff(binary[0], old, new, base, head, base_refused=refused, form=form, held=held)
    if not rendered:
        return None
    text = read_diff(diff_path)
    starts = [match.start() for match in DIFF_BLOCK.finditer(text)]
    pieces = [text[: starts[0]] if starts else text]
    for start, stop in zip(starts, [*starts[1:], len(text)], strict=True):
        block = text[start:stop]
        files = list(parse_unified_diff(block))
        pieces.append(rendered.get(files[0], block) if len(files) == 1 else block)
    atomic_write_text(diff_path, "".join(pieces))
    return places, patch_fingerprints(parse_unified_diff("".join(pieces)))

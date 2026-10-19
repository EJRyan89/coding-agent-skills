"""A run's diff and source snapshot: fetching the head, the snapshot, and changed documents."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from git_client import Runner
from github_client import GitHubError
from review_analyzers import reads_settings
from review_documents import EXTRACTED_WHEN_TEXT, ExtractionRefused, document_diff, document_format, extract, locations
from review_io import atomic_write_text, read_diff
from review_operation import validate_canary_pull
from review_run import PipelineError, Services
from review_runtime import (
    MAX_CHANGED_FILE_BYTES,
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


def _has_commit(checkout: Path, commit: str, git: Runner) -> bool:
    return git_in(checkout, git, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


_FETCH_LOCKS: dict[str, threading.Lock] = {}


_FETCH_LOCKS_GUARD = threading.Lock()


def _fetch_lock(checkout: Path) -> threading.Lock:
    """One lock per checkout: concurrent fetches into one repository contend for FETCH_HEAD and ref locks."""
    key = os.path.normcase(str(checkout.resolve()))
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


def _fetch_diff(
    repository: str, number: int, pull: dict[str, Any], diff_path: Path, services: Services, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Write the pull request's diff to `diff_path` once the pull is confirmed unmoved. Returns what `_write_diff`
    returns and the open review comments."""
    selector = f"{repository}#{number}"
    head = pull["headRefOid"]
    diff, undecodable = services.github.get_pull_diff(repository, number)
    # GitHub serves the diff by pull number, which follows pushes. Confirm the pull did not move after its
    # head was read, so a new diff is never archived under the old head SHA.
    current = validate_canary_pull(services.github.get_pull(repository, number), repository=repository, number=number)
    if (current["headRefOid"], current["baseRefOid"]) != (head, pull["baseRefOid"]):
        raise PipelineError(
            f"{selector} changed while it was being prepared (head {head[:12]} is now "
            f"{current['headRefOid'][:12]}); run prepare again"
        )
    parsed, changed, unsafe, patches = _write_diff(selector, diff, undecodable, diff_path, notes)
    comments = services.github.list_open_review_threads(repository, number)
    return parsed, changed, unsafe, patches, comments


def _write_diff(
    selector: str, diff: str, undecodable: int, diff_path: Path, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]]]:
    """Write a pull request's diff to `diff_path`. Returns the parsed diff, its changed paths, the changed paths no
    reviewer can be given (each noted), and their patch fingerprints."""
    atomic_write_text(diff_path, diff)
    if undecodable:
        notes.append(f"{undecodable} undecodable bytes replaced in the diff")
    parsed, unsafe = split_unified_diff(diff)
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
    return parsed, changed, unsafe, patches


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
) -> tuple[dict[str, tuple[int, str] | None], dict[str, Any], dict[str, Any]]:
    """Snapshot the head at `source`, from the checkout or else GitHub's tarball, leaving out what the repository's
    `exclude` globs match, probing a changed file over the size limit by the shipped suffixes and the repository's
    `unreviewed_suffixes`, and noting each changed document whose text it could not extract. Returns the symbolic
    links the snapshot leaves out that the pull request changes, each one noted, the snapshot's statistics: its
    source, files, bytes, and exclusions by reason, and the seconds spent fetching the head and materializing it, and
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
    dispatch: str,
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
    dispatch: str | None, manifest: dict[str, Any] | None, changed: list[str], renamed: Mapping[str, Sequence[str]]
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

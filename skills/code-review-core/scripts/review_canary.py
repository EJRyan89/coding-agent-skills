"""A fixture canary's pull request, read from a local directory instead of GitHub.

A fixture directory holds a change as two trees and the pull request around them:

    base/       the tree the pull request starts from
    head/       the tree it proposes
    pull.json   the pull request, validated by `validate_fixture_pull`

The suite's generic reviewer reviews a fixture, unless pull.json names a `manifest_path`: a specialists manifest in
the base tree, which `prepare` reads from the base commit as it reads a configured repository's `manifest_path`.

A fixture is trusted input, as the configuration is: it lives in the suite's source repository, outside anything that
ships, and no pull request's author writes it. It still takes the real path from the commits on. `fixture_change`
commits both trees, byte for byte, to a throwaway repository whose origin names the fixture's repository, so `prepare`
snapshots the head from that repository's objects as it snapshots a checkout's, with the same exclusions, and reads the
diff git computes between the two commits as GitHub would serve it. Nothing here calls GitHub.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from git_client import GitClient, GitError, Runner
from github_client import replace_undecodable
from review_config import ConfigurationError, validate_repository_identity
from review_io import PersistenceError, read_json
from review_records import RecordError, validate_record
from review_runtime import RuntimeContractError, _is_reparse_point, _safe_relative_path

FIXTURE_SCHEMA_VERSION = 1
PULL_FILE = "pull.json"
PULL_FIELDS = frozenset({"schema_version", "repository", "number", "title", "base_ref", "head_ref", "threads"})
# A reviewer manifest in the base tree, so a fixture can exercise specialists as a configured repository does.
OPTIONAL_PULL_FIELDS = frozenset({"manifest_path"})
THREAD_FIELDS = frozenset({"author", "path", "line", "outdated", "body", "url"})
# Who commits the fixture's trees; nothing reads it.
IDENTITY = ("-c", "user.name=code-review fixture", "-c", "user.email=fixture@example.invalid")


class FixtureError(ValueError):
    pass


@dataclass(frozen=True)
class FixtureChange:
    """A fixture's pull request as `prepare` reads one from GitHub, and the repository its commits are in."""

    name: str  # the owner/repo the fixture names
    repository: Path
    pull: dict[str, Any]  # the fields review_operation.PULL_FIELDS names
    diff: str
    undecodable: int  # bytes of the diff that were not UTF-8 and became U+FFFD
    comments: list[dict[str, Any]]  # as the request's github_comments
    manifest_path: str | None = None  # the reviewer manifest in the base tree, or None for the generic reviewer


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{PULL_FILE} {field} must be text")
    return value


def _positive(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FixtureError(f"{PULL_FILE} {field} must be a positive integer")
    return value


def _thread(value: Any, index: int) -> dict[str, Any]:
    field = f"threads[{index}]"
    if not isinstance(value, dict) or set(value) != THREAD_FIELDS:
        raise FixtureError(f"{PULL_FILE} {field} must have exactly {', '.join(sorted(THREAD_FIELDS))}")
    for name in ("author", "path", "body", "url"):
        _text(value[name], f"{field}.{name}")
    if value["line"] is not None:
        _positive(value["line"], f"{field}.line")
    if not isinstance(value["outdated"], bool):
        raise FixtureError(f"{PULL_FILE} {field}.outdated must be a boolean")
    return value


def validate_fixture_pull(value: Any) -> dict[str, Any]:
    """A fixture's pull.json, its repository normalized as a configured one is."""
    if not isinstance(value, dict) or not PULL_FIELDS <= set(value) <= PULL_FIELDS | OPTIONAL_PULL_FIELDS:
        raise FixtureError(
            f"{PULL_FILE} must have exactly {', '.join(sorted(PULL_FIELDS))}, and may have "
            f"{', '.join(sorted(OPTIONAL_PULL_FIELDS))}"
        )
    if value["schema_version"] != FIXTURE_SCHEMA_VERSION or isinstance(value["schema_version"], bool):
        raise FixtureError(f"{PULL_FILE} schema_version must be {FIXTURE_SCHEMA_VERSION}")
    try:
        repository = validate_repository_identity(value["repository"])
    except ConfigurationError as exc:
        raise FixtureError(f"{PULL_FILE} repository: {exc}") from exc
    _positive(value["number"], "number")
    for name in ("title", "base_ref", "head_ref"):
        _text(value[name], name)
    if not isinstance(value["threads"], list):
        raise FixtureError(f"{PULL_FILE} threads must be a list")
    threads = [_thread(thread, index) for index, thread in enumerate(value["threads"])]
    pull = {**value, "repository": repository, "threads": threads}
    if "manifest_path" in value:
        try:
            pull["manifest_path"] = _safe_relative_path(value["manifest_path"], f"{PULL_FILE} manifest_path")
        except RuntimeContractError as exc:
            raise FixtureError(str(exc)) from exc
    return pull


def validate_prior_record(value: Any, *, repository: str, number: int) -> dict[str, Any]:
    """The review a fixture re-review starts from: a valid first review of the same pull request."""
    try:
        record = validate_record(value)
    except RecordError as exc:
        raise FixtureError(f"The prior record is invalid: {exc}") from exc
    if (record["repository"], record["pull_request"]["number"]) != (repository, number):
        raise FixtureError(
            f"The prior record reviews {record['repository']}#{record['pull_request']['number']}, "
            f"not {repository}#{number}"
        )
    if record["review"]["version"] != 1:
        raise FixtureError("The prior record must be the pull request's first review, version 1")
    return record


def _tree_files(root: Path) -> list[tuple[str, Path]]:
    """Each file under `root` as (its POSIX path, its path), sorted. A link or another entry that is not a regular
    file or folder has no place in a fixture and is refused.

    Each entry is judged from the metadata its folder's listing already holds, reparse flag included, so the walk
    makes no file-system call per entry, and the POSIX path is built as a string rather than through pathlib."""
    if not root.is_dir() or _is_reparse_point(root):
        raise FixtureError(f"The fixture has no {root.name}/ tree")
    files: list[tuple[str, Path]] = []
    folders = [("", str(root))]
    while folders:
        prefix, folder = folders.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                relative = prefix + entry.name
                path, metadata = Path(entry.path), entry.stat(follow_symlinks=False)
                if _is_reparse_point(path, metadata):
                    raise FixtureError(f"The fixture holds a link or special file: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    folders.append((relative + "/", entry.path))
                elif stat.S_ISREG(metadata.st_mode):
                    files.append((relative, path))
                else:
                    raise FixtureError(f"The fixture holds a link or special file: {relative}")
    return sorted(files)


def _blob_stream(files: list[tuple[str, Path]]) -> bytes:
    """A fast-import stream of each file's bytes as a blob, asking for its id after each one."""
    blobs = [b"feature get-mark\n"]
    for number, (relative, path) in enumerate(files, start=1):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise FixtureError(f"Cannot read the fixture file {relative}: {exc}") from exc
        blobs.append(b"blob\nmark :%d\ndata %d\n%s\nget-mark :%d\n" % (number, len(content), content, number))
    return b"".join(blobs)


def _index_path(relative: str) -> bytes:
    """`relative` as update-index reads it on stdin, which carries only Unicode text."""
    try:
        return relative.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FixtureError(f"The fixture holds a path that is not Unicode: {relative!r}") from exc


class _Repository:
    def __init__(self, path: Path, runner: Runner) -> None:
        self.path = path
        self.client = GitClient(runner)

    def git(self, *arguments: str, input_bytes: bytes | None = None) -> str:
        try:
            return self.client.output(arguments, directory=self.path, input_bytes=input_bytes)
        except GitError as exc:
            raise FixtureError(f"git {arguments[0]} failed for the fixture: {exc}") from exc

    def commit(self, tree: Path, message: str, parent: str | None = None) -> str:
        """A commit of exactly the files under `tree`, each as its bytes on disk: fast-import stores a blob as given,
        with no attribute, line-ending setting, or filter. Every file is a regular, non-executable one.

        Two git commands take the whole tree on stdin. fast-import writes every blob into one pack, since thousands
        of loose objects take minutes to write and to remove on Windows. update-index --index-info then places each
        blob at its path; it skips a path git refuses, such as one inside `.git`, and still succeeds, so `--verbose`
        lists each path it adds and a path missing from that list is refused here."""
        files = _tree_files(tree)
        paths = [_index_path(relative) for relative, _ in files]
        stream = _blob_stream(files)
        self.git("read-tree", "--empty")
        blobs = self.git("fast-import", "--quiet", "--cat-blob-fd=1", input_bytes=stream).split()
        if len(blobs) != len(files):
            raise FixtureError("git fast-import printed an unexpected listing")
        entries = b"".join(
            b"100644 blob %s\t%s\0" % (blob.encode("ascii"), path) for path, blob in zip(paths, blobs, strict=True)
        )
        added = self.git("update-index", "--verbose", "-z", "--add", "--index-info", input_bytes=entries)
        if added != "".join(f"add '{relative}'\n" for relative, _ in files):
            listed = set(added.splitlines())
            refused = next((relative for relative, _ in files if f"add '{relative}'" not in listed), None)
            if refused is None:
                raise FixtureError("git update-index printed an unexpected listing")
            raise FixtureError(f"git update-index refused the fixture path {refused}")
        tree_id = self.git("write-tree").strip()
        parents = ("-p", parent) if parent else ()
        return self.git(*IDENTITY, "commit-tree", "--no-gpg-sign", tree_id, *parents, "-m", message).strip()


def _comments(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The threads as the request's review comments, numbered C1, C2, ... in order, as the GitHub client numbers
    the open threads a person started."""
    return [{"id": f"C{index}", **thread} for index, thread in enumerate(threads, start=1)]


@contextmanager
def fixture_change(directory: Path, runner: Runner) -> Iterator[FixtureChange]:
    """The fixture's pull request, its commits in a throwaway repository that exists until the block ends, unless the
    caller moves it elsewhere first."""
    directory = directory.resolve()
    try:
        pull = validate_fixture_pull(read_json(directory / PULL_FILE))
    except PersistenceError as exc:
        raise FixtureError(str(exc)) from exc
    # TemporaryDirectory's cleanup also removes the object files git leaves read-only, which Windows will not delete.
    with tempfile.TemporaryDirectory(prefix="code-review-fixture-", ignore_cleanup_errors=True) as temporary:
        # A folder of its own, so prepare can move the repository into the run, where a lazy snapshot's reviewers
        # fetch from it until finalize removes the run.
        repository = Path(temporary).resolve() / "repository"
        repository.mkdir()
        store = _Repository(repository, runner)
        store.git("init", "--quiet")
        store.git("remote", "add", "origin", f"https://github.com/{pull['repository']}.git")
        base = store.commit(directory / "base", "base")
        head = store.commit(directory / "head", "head", parent=base)
        # The options a user's configuration could otherwise change, so the diff is git's default, as GitHub's is.
        raw = store.git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--unified=3",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            head,
        )
        diff, undecodable = replace_undecodable(raw)
        yield FixtureChange(
            name=pull["repository"],
            repository=repository,
            pull={
                "number": pull["number"],
                "title": pull["title"],
                "url": f"https://github.com/{pull['repository']}/pull/{pull['number']}",
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": pull["base_ref"],
                "baseRefOid": base,
                "headRefOid": head,
                "headRefName": pull["head_ref"],
                "mergedAt": None,
            },
            diff=diff,
            undecodable=undecodable,
            comments=_comments(pull["threads"]),
            manifest_path=pull.get("manifest_path"),
        )

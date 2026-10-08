"""A fixture canary's pull request, read from a local directory instead of GitHub.

A fixture directory holds a change as two trees and the pull request around them:

    base/       the tree the pull request starts from
    head/       the tree it proposes
    pull.json   the pull request, validated by `validate_fixture_pull`

A fixture is trusted input, as the configuration is: it lives in the suite's source repository, outside anything that
ships, and no pull request's author writes it. It still takes the real path from the commits on. `fixture_change`
commits both trees, byte for byte, to a throwaway repository whose origin names the fixture's repository, so `prepare`
snapshots the head from that repository's objects as it snapshots a checkout's, with the same exclusions, and reads the
diff git computes between the two commits as GitHub would serve it. Nothing here calls GitHub.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
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
from review_runtime import _is_reparse_point

FIXTURE_SCHEMA_VERSION = 1
PULL_FILE = "pull.json"
PULL_FIELDS = frozenset({"schema_version", "repository", "number", "title", "base_ref", "head_ref", "threads"})
THREAD_FIELDS = frozenset({"author", "path", "line", "outdated", "body", "url"})
# Who commits the fixture's trees; nothing reads it.
IDENTITY = ("-c", "user.name=code-review fixture", "-c", "user.email=fixture@example.invalid")
# Characters of paths one git command takes, well under the 32,767 of a Windows command line.
ARGUMENT_BUDGET = 24_000


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
    if not isinstance(value, dict) or set(value) != PULL_FIELDS:
        raise FixtureError(f"{PULL_FILE} must have exactly {', '.join(sorted(PULL_FIELDS))}")
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
    return {**value, "repository": repository, "threads": threads}


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
    file or folder has no place in a fixture and is refused."""
    if not root.is_dir() or _is_reparse_point(root):
        raise FixtureError(f"The fixture has no {root.name}/ tree")
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(root, followlinks=False):
        for name in [*directories, *names]:
            child = Path(current) / name
            if _is_reparse_point(child) or not (child.is_dir() if name in directories else child.is_file()):
                raise FixtureError(f"The fixture holds a link or special file: {child.relative_to(root).as_posix()}")
        files.extend((Path(current, name).relative_to(root).as_posix(), Path(current, name)) for name in names)
    return sorted(files)


def _batches(arguments: Sequence[tuple[str, ...]]) -> Iterator[list[str]]:
    """The argument groups, flattened into lists that each fit one command line."""
    batch: list[str] = []
    size = 0
    for group in arguments:
        length = sum(len(argument) + 3 for argument in group)
        if batch and size + length > ARGUMENT_BUDGET:
            yield batch
            batch, size = [], 0
        batch.extend(group)
        size += length
    if batch:
        yield batch


class _Repository:
    def __init__(self, path: Path, runner: Runner) -> None:
        self.path = path
        self.client = GitClient(runner)

    def git(self, *arguments: str) -> str:
        try:
            return self.client.output(arguments, directory=self.path)
        except GitError as exc:
            raise FixtureError(f"git {arguments[0]} failed for the fixture: {exc}") from exc

    def commit(self, tree: Path, message: str, parent: str | None = None) -> str:
        """A commit of exactly the files under `tree`, each as its bytes on disk: hash-object --no-filters applies no
        attribute, line-ending setting, or filter. Every file is a regular, non-executable one."""
        files = _tree_files(tree)
        self.git("read-tree", "--empty")
        blobs: list[str] = []
        for batch in _batches([(str(path),) for _, path in files]):
            printed = self.git("hash-object", "-w", "--no-filters", "--", *batch).split()
            if len(printed) != len(batch):
                raise FixtureError("git hash-object printed an unexpected listing")
            blobs.extend(printed)
        entries = [("--cacheinfo", "100644", blob, relative) for (relative, _), blob in zip(files, blobs, strict=True)]
        for batch in _batches(entries):
            self.git("update-index", "--add", *batch)
        tree_id = self.git("write-tree").strip()
        parents = ("-p", parent) if parent else ()
        return self.git(*IDENTITY, "commit-tree", "--no-gpg-sign", tree_id, *parents, "-m", message).strip()


def _comments(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The threads as the request's review comments, numbered C1, C2, ... in order, as the GitHub client numbers
    the open threads a person started."""
    return [{"id": f"C{index}", **thread} for index, thread in enumerate(threads, start=1)]


@contextmanager
def fixture_change(directory: Path, runner: Runner) -> Iterator[FixtureChange]:
    """The fixture's pull request, its commits in a throwaway repository that exists until the block ends."""
    directory = directory.resolve()
    try:
        pull = validate_fixture_pull(read_json(directory / PULL_FILE))
    except PersistenceError as exc:
        raise FixtureError(str(exc)) from exc
    # TemporaryDirectory's cleanup also removes the object files git leaves read-only, which Windows will not delete.
    with tempfile.TemporaryDirectory(prefix="code-review-fixture-", ignore_cleanup_errors=True) as temporary:
        repository = Path(temporary).resolve()
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
        )

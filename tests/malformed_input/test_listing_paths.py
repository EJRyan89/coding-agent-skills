"""Every reader of a path a GitHub listing names takes each path git can hold, a blank one included.

Git takes a file named by Unicode whitespace alone, such as a no-break space, so GitHub lists one in a pull request's
files, a comparison's files, a tree, and a review thread. Each reader here takes such a path through
`FieldReader.repository_path`, which refuses only an empty string, and returns it as listed; a reader that took it
through `string` refused the whole listing, and with it the review or the tracker row.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from edge_values import PATHS
from reader_table import REPOSITORY

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("update-pr-dashboard")))

import pr_change
import review_github
import review_runtime
from typed_fields import FieldError

BLOB = "a" * 40


def changed_file(path: str) -> dict[str, Any]:
    """A pull request's or a comparison's files entry for a file renamed from `path` to `path`."""
    return {"filename": path, "previous_filename": path, "status": "renamed", "changes": 1, "sha": BLOB}


def thread(path: str) -> dict[str, Any]:
    """An unresolved review thread a person started on `path`."""
    comment = {"author": {"__typename": "User", "login": "octocat"}, "body": "Why?", "url": "https://example.invalid/1"}
    return {"isResolved": False, "isOutdated": False, "line": 1, "path": path, "comments": {"nodes": [comment]}}


@pytest.mark.parametrize("path", PATHS)
def test_each_listing_reader_takes_every_path_git_can_hold(path: str) -> None:
    pulled = review_github._changed_file(changed_file(path), "files[0]")
    assert (path, path) == (pulled["filename"], pulled["previous_filename"])
    opened = review_github._open_thread_comment(thread(path), "threads[0]")
    assert opened is not None
    assert path == opened["path"]
    tree = {"truncated": False, "tree": [{"type": "blob", "path": path, "mode": "100644", "sha": BLOB}]}
    assert [(path, "file", BLOB)] == list(review_runtime._listed_blobs(tree))
    assert {path: "100644"} == pr_change.tree_modes({"truncated": False, "tree": [{"path": path, "mode": "100644"}]})
    assert ((path, "renamed", path, BLOB),) == pr_change.changed_files({"files": [changed_file(path)]})


def test_each_listing_reader_still_refuses_an_empty_path() -> None:
    for read in (
        lambda: review_github._changed_file(changed_file(""), "files[0]"),
        lambda: review_github._open_thread_comment(thread(""), "threads[0]"),
        lambda: review_runtime._listed_blobs(
            {"truncated": False, "tree": [{"type": "blob", "path": "", "mode": "100644", "sha": BLOB}]}
        ),
    ):
        with pytest.raises(FieldError, match="must be a non-empty string, found an empty string"):
            read()
    # The tracker's readers answer None for a listing they cannot read, so the row is refreshed, never matched.
    assert pr_change.tree_modes({"truncated": False, "tree": [{"path": "", "mode": "100644"}]}) is None
    assert pr_change.changed_files({"files": [changed_file("")]}) is None

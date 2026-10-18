"""Regression tests for the one reader of a GitHub repository from a git remote's URL."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_remote import github_repository

# Runs no test of its own; the test functions below check through its assertions, as unittest classes do.
CHECK = unittest.TestCase()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://github.com/owner/repo.git", "owner/repo"),
        ("https://user@github.com/Owner/my.repo.git", "Owner/my.repo"),
        ("https://GitHub.com/owner/repo", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("ssh://git@github.com:22/owner/repo", "owner/repo"),
        pytest.param("  https://github.com/owner/repo.git\n", "owner/repo", id="padded-with-spaces-and-a-newline"),
    ],
)
def test_each_github_com_form_names_its_repository_as_written(url: str, expected: str) -> None:
    CHECK.assertEqual(expected, github_repository(url))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/github.com/owner/repo",
        "https://example.invalid/mirror/github.com:owner/repo.git",
        "git@example.invalid:github.com/owner/repo",
        "ssh://git@example.invalid/github.com/owner/repo.git",
        "https://github.com.example.invalid/owner/repo",
        "https://github.com@example.invalid/owner/repo",
        "https://example.invalid/?github.com/owner/repo",
        "https://gitlab.com/owner/repo.git",
        "https://github.com/owner/repo/extra",
        "https://github.com/owner",
        "github.com/owner/repo",
        "C:/remotes/repo.git",
        pytest.param("", id="empty"),
    ],
)
def test_a_url_on_another_host_or_in_another_shape_is_refused(url: str) -> None:
    CHECK.assertIsNone(github_repository(url))

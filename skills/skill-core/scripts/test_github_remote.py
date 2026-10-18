"""Regression tests for the one reader of a GitHub repository from a git remote's URL."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_remote import github_repository


class GitHubRepositoryTests(unittest.TestCase):
    def test_each_github_com_form_names_its_repository_as_written(self) -> None:
        for url, expected in (
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
            ("  https://github.com/owner/repo.git\n", "owner/repo"),
        ):
            with self.subTest(url=url):
                self.assertEqual(expected, github_repository(url))

    def test_a_url_on_another_host_or_in_another_shape_is_refused(self) -> None:
        for url in (
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
            "",
        ):
            with self.subTest(url=url):
                self.assertIsNone(github_repository(url))

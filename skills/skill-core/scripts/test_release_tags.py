"""Regression tests for the one definition of a release tag and the lookup of the last release a commit holds."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient
from release_tags import highest_release, is_release_tag, last_release, main


class ReleaseTagTests(unittest.TestCase):
    def test_a_release_tag_is_three_ascii_numbers_and_nothing_after_them(self) -> None:
        for name in ("v0.1.0", "v10.20.30", "v01.2.3", "v1.0.000"):
            with self.subTest(name=name):
                self.assertTrue(is_release_tag(name))
        # A pre-release suffix, a build suffix, a missing or extra component, a missing v, digits outside ASCII, and a
        # name with a line break are not releases.
        for name in (
            "v1.0.0-rc.1",
            "v1.0.0+build",
            "v1.0",
            "v1.0.0.0",
            "1.0.0",
            "V1.0.0",
            "v2x.0.0",
            f"v{chr(0x661)}.0.0",
            "v1.0.0\n",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_release_tag(name))

    def test_releases_are_ordered_by_their_numbers_not_their_text(self) -> None:
        self.assertEqual("v0.10.0", highest_release(["v0.9.0", "v0.10.0", "v0.2.0"]))
        self.assertEqual("v2.0.0", highest_release(["v1.99.99", "v2.0.0", "v1.100.0"]))
        self.assertEqual("v0.0.10", highest_release(["v0.0.9", "v0.0.10"]))
        # A component longer than any machine integer still compares exactly.
        self.assertEqual("v1" + "0" * 40 + ".0.0", highest_release(["v9.0.0", "v1" + "0" * 40 + ".0.0"]))
        self.assertEqual("v1.0.0", highest_release(["v01.0.0", "v1.0.0"]))

    def test_other_tags_are_left_out_and_none_means_no_release(self) -> None:
        self.assertEqual("v1.0.0", highest_release(["v2.0.0-rc.1", "v1.0.0", "nightly", "v3"]))
        self.assertIsNone(highest_release(["v2.0.0-rc.1", "nightly"]))
        self.assertIsNone(highest_release([]))


class LastReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "-b", "main")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], capture_output=True, text=True, check=True
        ).stdout.strip()

    def commit(self, message: str, *tags: str) -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message)
        for tag in tags:
            self.git("tag", tag)
        return self.git("rev-parse", "HEAD")

    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(arguments))
        return code, output.getvalue()

    def test_the_highest_reachable_release_wins_over_a_nearer_tag_of_another_name(self) -> None:
        first = self.commit("first", "v0.1.0")
        self.commit("second", "v0.2.0")
        self.git("tag", "-a", "-m", "annotated", "v0.10.0", "HEAD")
        self.commit("third", "v0.11.0-rc.1", "nightly")
        self.git("branch", "side", first)
        self.git("checkout", "-q", "side")
        self.commit("off main", "v9.0.0")
        client = GitClient()
        self.assertEqual("v0.10.0", last_release(client, self.root, "main"))
        self.assertEqual("v0.1.0", last_release(client, self.root, first))
        self.assertEqual("v9.0.0", last_release(client, self.root, "side"))
        self.assertEqual((0, "RELEASE v0.10.0\n"), self.run_main(str(self.root), "refs/heads/main"))

    def test_a_commit_no_release_reaches_has_none(self) -> None:
        untagged = self.commit("untagged", "v1.0.0-rc.1")
        self.commit("released", "v1.0.0")
        self.assertIsNone(last_release(GitClient(), self.root, untagged))
        self.assertEqual((0, "NO_RELEASE\n"), self.run_main(str(self.root), untagged))

    def test_a_commit_the_repository_lacks_is_a_failed_line(self) -> None:
        self.commit("first", "v0.1.0")
        code, output = self.run_main(str(self.root), "0" * 40)
        self.assertEqual(1, code)
        self.assertTrue(output.startswith("FAILED git for-each-ref: "), output)
        self.assertEqual(1, len(output.splitlines()), output)

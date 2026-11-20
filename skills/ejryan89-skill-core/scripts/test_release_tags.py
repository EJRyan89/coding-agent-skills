"""Regression tests for the one definition of a release tag, the lookup of the last release a commit holds, and the
releases naming a commit."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient, GitError, GitResult, subprocess_runner
from release_tags import breaking_component, highest_release, is_release_tag, last_release, main, releases_naming

# The tag fields a Git whose ref-filter peels one tag level gives, as Git did before 2.42 for --points-at and for the
# `%(*...)` fields.
ONE_LEVEL_FIELDS = ("%(objectname)", "%(objecttype)", "%(*objectname)", "%(*objecttype)", "%(refname)")


def one_level_git(command: Sequence[str], timeout: float) -> GitResult:
    """GitClient's runner as an older Git: `for-each-ref` over tags, by `--points-at` and in its `%(*...)` fields,
    peels one tag level alone, so a tag of a tag reads as pointing at a tag; every other command is real git's."""
    arguments = list(command)
    if "for-each-ref" not in arguments:
        return subprocess_runner(command, timeout)
    directory = arguments[arguments.index("-C") + 1]

    def git(*words: str) -> str:
        return subprocess.run(
            ["git", "-C", directory, *words], capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout

    tags = []
    for line in git("for-each-ref", "--format=%(objectname) %(objecttype) %(refname)", "refs/tags/").splitlines():
        object_name, object_type, ref = line.split(" ", 2)
        inner = {"object": "", "type": ""}
        if object_type == "tag":
            header = git("cat-file", "tag", object_name).split("\n\n", 1)[0]
            inner = dict(field.split(" ", 1) for field in header.splitlines())
        tags.append(
            dict(zip(ONE_LEVEL_FIELDS, (object_name, object_type, inner["object"], inner["type"], ref), strict=True))
        )
    points_at = [word.removeprefix("--points-at=") for word in arguments if word.startswith("--points-at=")]
    if points_at:
        commit = git("rev-parse", "--verify", points_at[0]).strip()
        tags = [tag for tag in tags if commit in {tag["%(objectname)"], tag["%(*objectname)"]}]
    form = next(word.removeprefix("--format=") for word in arguments if word.startswith("--format="))
    lines = []
    for tag in tags:
        line = form.replace("%00", "\0")
        for field, value in tag.items():
            line = line.replace(field, value)
        lines.append(line + "\n")
    return GitResult(0, "".join(lines), "")


def answering(for_each_ref: str, rev_parse: str) -> GitClient:
    """A GitClient whose git names the commit by forty 1s, answers `for-each-ref` and an unverified `rev-parse` as
    given, and refuses everything else."""

    def runner(command: Sequence[str], timeout: float) -> GitResult:
        if "for-each-ref" in command:
            return GitResult(0, for_each_ref, "")
        if "--verify" in command:
            return GitResult(0, "1" * 40 + "\n", "")
        if "rev-parse" in command:
            return GitResult(0, rev_parse, "")
        raise AssertionError(f"unexpected git command: {command}")

    return GitClient(runner)


class ReleaseTagTests(unittest.TestCase):
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


class RaisesTests(unittest.TestCase):
    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--raises", *arguments])
        return code, output.getvalue()

    def test_the_breaking_component_is_the_major_or_the_minor_while_the_major_is_zero(self) -> None:
        self.assertEqual((0, 13), breaking_component((0, 13, 2)))
        self.assertEqual((2, 0), breaking_component((2, 7, 1)))

    def test_a_release_raises_only_the_breaking_component(self) -> None:
        self.assertEqual((0, "RAISES\n"), self.run_main("untagged", "v0.1.0"))
        self.assertEqual((0, "WITHIN\n"), self.run_main("untagged", "v0.0.4"))
        self.assertEqual((0, "RAISES\n"), self.run_main("v0.9.0", "v0.10.0"))
        self.assertEqual((0, "WITHIN\n"), self.run_main("v0.2.3", "v0.2.9"))
        self.assertEqual((0, "RAISES\n"), self.run_main("v0.2.3", "v1.0.0"))
        self.assertEqual((0, "WITHIN\n"), self.run_main("v1.5.0", "v1.99.0"))
        self.assertEqual((0, "WITHIN\n"), self.run_main("v2.0.0", "v1.0.0"))

    def test_a_number_past_a_machine_integer_compares_exactly(self) -> None:
        # Nineteen digits and more overflow Bash's integers, which update.sh once compared these with.
        huge = "1" + "0" * 40
        self.assertEqual((0, "RAISES\n"), self.run_main(f"v{huge[:-1]}.0.0", f"v{huge}.0.0"))
        self.assertEqual((0, "WITHIN\n"), self.run_main(f"v{huge}.0.0", f"v{huge}.9.0"))
        self.assertEqual((0, "RAISES\n"), self.run_main(f"v0.{huge[:-1]}.0", f"v0.{huge}.0"))

    def test_a_name_that_is_not_a_release_is_a_failed_line(self) -> None:
        self.assertEqual((1, "FAILED not a release tag: v1.0.0-rc.1\n"), self.run_main("v1.0.0-rc.1", "v2.0.0"))
        self.assertEqual((1, "FAILED not a release tag: untagged\n"), self.run_main("v1.0.0", "untagged"))


class LastReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "-b", "main")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], capture_output=True, text=True, encoding="utf-8", check=True
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

    def test_a_tag_whose_name_holds_a_character_splitlines_breaks_on_is_one_name(self) -> None:
        # U+2028 and U+0085 end a line to str.splitlines(), but git ends each ref it lists with a newline alone.
        self.commit("first", "v0.1.0", f"note{chr(0x2028)}v9.0.0", f"note{chr(0x85)}v8.0.0")
        self.assertEqual("v0.1.0", last_release(GitClient(), self.root, "HEAD"))
        self.assertEqual((0, "RELEASE v0.1.0\n"), self.run_main(str(self.root), "HEAD"))

    def test_a_commit_the_repository_lacks_is_a_failed_line(self) -> None:
        self.commit("first", "v0.1.0")
        code, output = self.run_main(str(self.root), "0" * 40)
        self.assertEqual(1, code)
        self.assertTrue(output.startswith("FAILED git for-each-ref: "), output)
        self.assertEqual(1, len(output.splitlines()), output)

    def test_every_release_naming_the_commit_is_listed_in_release_order_whatever_tags_peel_to_it(self) -> None:
        # An older release, an annotated one, and a tag of a tag all name the commit; a pre-release and another tag
        # there, a release on its parent, and one on a later commit do not.
        first = self.commit("first", "v0.1.0")
        commit = self.commit("second", "v0.10.0", "v0.2.0", "v0.10.1-rc.1", "nightly")
        self.git("tag", "-a", "-m", "annotated", "v0.9.0", commit)
        self.git("tag", "-a", "-m", "nested", "v0.11.0", "refs/tags/v0.9.0")
        self.commit("third", "v1.0.0")
        client = GitClient()
        self.assertEqual(["v0.2.0", "v0.9.0", "v0.10.0", "v0.11.0"], releases_naming(client, self.root, commit))
        self.assertEqual(["v0.1.0"], releases_naming(client, self.root, first))
        self.assertEqual(
            (0, "RELEASE v0.2.0\nRELEASE v0.9.0\nRELEASE v0.10.0\nRELEASE v0.11.0\n"),
            self.run_main("--naming", str(self.root), commit),
        )

    def test_a_commit_no_release_names_has_none_though_a_release_reaches_it(self) -> None:
        self.commit("released", "v1.0.0")
        untagged = self.commit("untagged", "v1.1.0-rc.1")
        self.assertEqual("v1.0.0", last_release(GitClient(), self.root, untagged))
        self.assertEqual([], releases_naming(GitClient(), self.root, untagged))
        self.assertEqual((0, "NO_RELEASE\n"), self.run_main("--naming", str(self.root), untagged))

    def test_a_commit_the_repository_lacks_names_no_release_but_is_a_failed_line(self) -> None:
        # Never an empty list: a pin to a commit the clone lost is not read as naming no release.
        self.commit("first", "v0.1.0")
        with self.assertRaises(GitError):
            releases_naming(GitClient(), self.root, "0" * 40)
        code, output = self.run_main("--naming", str(self.root), "0" * 40)
        self.assertEqual(1, code)
        self.assertTrue(output.startswith("FAILED git for-each-ref: "), output)
        self.assertEqual(1, len(output.splitlines()), output)

    def test_a_release_behind_any_number_of_tag_objects_is_listed_on_a_git_that_peels_one_level(self) -> None:
        # A release, a tag of it, and a tag of that all name the commit; a release tag of a tree, and a tag of that,
        # name none; a release and a tag of it on a later commit name only that one.
        commit = self.commit("released")
        self.git("tag", "-a", "-m", "release", "v1.0.0", commit)
        self.git("tag", "-a", "-m", "nested", "v1.0.1", "refs/tags/v1.0.0")
        self.git("tag", "-a", "-m", "nested again", "v1.0.2", "refs/tags/v1.0.1")
        self.git("tag", "-a", "-m", "tree", "v3.0.0", f"{commit}^{{tree}}")
        self.git("tag", "-a", "-m", "nested tree", "v3.0.1", "refs/tags/v3.0.0")
        later = self.commit("later")
        self.git("tag", "-a", "-m", "release", "v2.0.0", later)
        self.git("tag", "-a", "-m", "nested", "v2.0.1", "refs/tags/v2.0.0")
        # The older Git this stands for reads the outer tag of a nested release as pointing at a tag.
        listing = ["git", "-C", str(self.root), "for-each-ref", "--format=%(*objecttype) %(refname)", "refs/tags/"]
        self.assertIn("tag refs/tags/v1.0.2", one_level_git(listing, 60).stdout.splitlines())
        for client in (GitClient(one_level_git), GitClient()):
            with self.subTest(client=client.runner.__name__):
                self.assertEqual(["v1.0.0", "v1.0.1", "v1.0.2"], releases_naming(client, self.root, commit))
                self.assertEqual(["v2.0.0", "v2.0.1"], releases_naming(client, self.root, later))

    def broken_tag(self, name: str) -> None:
        """An annotated tag whose target the repository has lost, which git reads as `fatal: bad tag`."""
        (self.root / "gone.txt").write_text("gone", encoding="utf-8")
        target = self.git("hash-object", "-w", "gone.txt")
        self.git("tag", "-a", "-m", "broken", name, target)
        (self.root / ".git" / "objects" / target[:2] / target[2:]).unlink()

    def test_a_broken_tag_of_another_name_leaves_the_releases_naming_the_commit_listed(self) -> None:
        commit = self.commit("released")
        self.git("tag", "-a", "-m", "release", "v1.0.0", commit)
        self.broken_tag("nightly")
        for client in (GitClient(one_level_git), GitClient()):
            with self.subTest(client=client.runner.__name__):
                self.assertEqual(["v1.0.0"], releases_naming(client, self.root, commit))

    def test_a_broken_release_tag_fails_closed_never_reads_as_no_release(self) -> None:
        # Which commit a release tag whose target is lost names is unknown, so its pin's owner is never guessed.
        commit = self.commit("released")
        self.git("tag", "-a", "-m", "release", "v1.0.0", commit)
        self.broken_tag("v1.0.1")
        with self.assertRaises(GitError):
            releases_naming(GitClient(), self.root, commit)

    def test_a_tag_listing_that_is_not_its_fields_raises_never_reads_as_no_release(self) -> None:
        with self.assertRaises(GitError):
            releases_naming(answering(f"{'1' * 40} refs/tags/v1.0.0\n", ""), self.root, "HEAD")

    def test_a_peel_that_answers_another_count_of_names_raises_never_reads_as_no_release(self) -> None:
        nested = "\0".join(("2" * 40, "3" * 40, "tag", "refs/tags/v1.0.0"))
        with self.assertRaises(GitError):
            releases_naming(answering(f"{nested}\n", ""), self.root, "HEAD")
        self.assertEqual(["v1.0.0"], releases_naming(answering(f"{nested}\n", "1" * 40 + "\n"), self.root, "HEAD"))


@pytest.mark.parametrize(
    ("name", "released"),
    [
        ("v0.1.0", True),
        ("v10.20.30", True),
        ("v01.2.3", True),
        ("v1.0.000", True),
        # A pre-release suffix, a build suffix, a missing or extra component, a missing v, digits outside ASCII, and a
        # name with a line break are not releases.
        ("v1.0.0-rc.1", False),
        ("v1.0.0+build", False),
        ("v1.0", False),
        ("v1.0.0.0", False),
        ("1.0.0", False),
        ("V1.0.0", False),
        ("v2x.0.0", False),
        pytest.param(f"v{chr(0x661)}.0.0", False, id="arabic-indic-digit"),
        pytest.param("v1.0.0\n", False, id="v1.0.0-newline"),
    ],
)
def test_a_release_tag_is_three_ascii_numbers_and_nothing_after_them(name: str, released: bool) -> None:
    assert released is is_release_tag(name)

"""Regression tests for the one check of how a main checkout differs from a clean one of its default branch at the
commit its remote's default branch holds."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_writer
from git_client import GitClient, GitError, GitResult
from hub_state import (
    GUARD_SETTING,
    RECORD_NAME,
    Difference,
    RecordError,
    changed_paths,
    changes,
    differences,
    lapse,
    read_record,
)


class HubTestCase(unittest.TestCase):
    """A hub cloned from a bare origin, on main at origin/main with nothing changed."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hub state "))).resolve()
        self.origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        self.git(self.root, "clone", "-q", str(self.origin), str(seed))
        self.commit(seed, "README.md", "first")
        self.git(seed, "push", "-q", "origin", "main")
        self.hub = self.root / "the hub"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.hub))
        self.seed = seed

    def git(self, directory: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.strip()

    def commit(self, checkout: Path, name: str, message: str) -> None:
        (checkout / name).write_text(f"{message}\n", encoding="utf-8")
        self.git(checkout, "add", name)
        self.git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)

    def found(self) -> list[Difference]:
        return differences(GitClient(), self.hub, "main")


class DifferenceTests(HubTestCase):
    def test_a_clean_hub_on_main_at_origin_s_main_differs_in_nothing(self) -> None:
        self.assertEqual([], self.found())

    def test_a_tracked_change_and_an_untracked_file_are_named(self) -> None:
        (self.hub / "README.md").write_text("edited\n", encoding="utf-8")
        (self.hub / "stray file.txt").write_text("stray\n", encoding="utf-8")
        self.assertEqual(
            [Difference("changed", "it has 2 uncommitted or untracked paths: README.md, stray file.txt")],
            self.found(),
        )

    def test_past_the_named_paths_the_rest_are_counted(self) -> None:
        for name in ("a", "b", "c", "d", "e"):
            (self.hub / name).write_text(name, encoding="utf-8")
        self.assertEqual(
            [Difference("changed", "it has 5 uncommitted or untracked paths: a, b, c, and 2 more")], self.found()
        )

    def test_an_ignored_file_is_no_change(self) -> None:
        self.commit(self.seed, ".gitignore", "ignored.txt")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "pull", "-q", "--ff-only", "origin", "main")
        (self.hub / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        self.assertEqual([], self.found())

    def test_a_hub_on_another_branch_is_named_with_it(self) -> None:
        self.git(self.hub, "switch", "-q", "-c", "feat/stray")
        self.assertEqual([Difference("branch", "it is on feat/stray, not main")], self.found())

    def test_a_detached_hub_is_named_detached(self) -> None:
        self.git(self.hub, "switch", "-q", "--detach")
        self.assertEqual([Difference("branch", "it is on a detached HEAD, not main")], self.found())

    def test_a_commit_made_in_the_hub_is_counted_ahead(self) -> None:
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.assertEqual([Difference("ahead", "it is 1 commit ahead of origin/main")], self.found())

    def test_a_hub_origin_moved_past_is_counted_behind(self) -> None:
        self.commit(self.seed, "a.txt", "second")
        self.commit(self.seed, "b.txt", "third")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "fetch", "-q", "origin")
        self.assertEqual([Difference("behind", "it is 2 commits behind origin/main")], self.found())

    def test_a_hub_that_diverged_is_counted_both_ways(self) -> None:
        self.commit(self.seed, "a.txt", "second")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "fetch", "-q", "origin")
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.assertEqual(
            [
                Difference("ahead", "it is 1 commit ahead of origin/main"),
                Difference("behind", "it is 1 commit behind origin/main"),
            ],
            self.found(),
        )

    def test_a_hub_with_no_remote_tracking_ref_says_so(self) -> None:
        self.git(self.hub, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual([Difference("unknown", "git holds no origin/main to compare it with")], self.found())

    def test_a_branch_or_tag_named_like_the_tracking_ref_is_not_read_for_it(self) -> None:
        self.git(self.hub, "update-ref", "-d", "refs/remotes/origin/main")
        self.git(self.hub, "tag", "origin/main")
        self.git(self.hub, "branch", "remotes/origin/main")
        self.assertEqual([Difference("unknown", "git holds no origin/main to compare it with")], self.found())

    def test_every_difference_is_reported_together(self) -> None:
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.git(self.hub, "switch", "-q", "-c", "feat/stray")
        (self.hub / "README.md").write_text("edited\n", encoding="utf-8")
        self.assertEqual(
            ["changed", "branch", "ahead"],
            [difference.kind for difference in self.found()],
        )

    if sys.platform != "win32":  # Windows refuses a file name holding a line break

        def test_a_path_holding_a_line_break_is_named_on_one_line(self) -> None:
            (self.hub / "two\nlines").write_text("x", encoding="utf-8")
            self.assertEqual([Difference("changed", "it has 1 uncommitted or untracked path: two lines")], self.found())


class ReadingTests(unittest.TestCase):
    def test_a_rename_names_its_new_path_once(self) -> None:
        self.assertEqual(["new", "x"], changed_paths(["R  new", "old", "?? x"]))
        self.assertEqual(["copy"], changed_paths([" C copy", "source"]))

    def test_a_checkout_git_cannot_read_raises(self) -> None:
        def run(command: Sequence[str], timeout: float) -> GitResult:
            return GitResult(128, "", "fatal: not a git repository")

        with self.assertRaises(GitError):
            differences(GitClient(run), Path("/nowhere"), "main")

    def test_counts_git_does_not_give_raise(self) -> None:
        for counts in ("x\n", "1\n", "1\t\n", "\u0661\t0\n"):
            answers = iter(["", "main\n", "c0ffee\0refs/remotes/origin/main\n", counts])

            def run(command: Sequence[str], timeout: float, answers: Iterator[str] = answers) -> GitResult:
                return GitResult(0, next(answers), "")

            with self.subTest(counts=counts), self.assertRaises(GitError):
                differences(GitClient(run), Path("/hub"), "main")


class ConfigurationCase(HubTestCase):
    """A hub whose configuration a tick records in its git directory."""

    def setUp(self) -> None:
        super().setUp()
        self.record = self.hub / ".git" / RECORD_NAME

    def configure(self, *arguments: str) -> None:
        self.git(self.hub, "config", "--local", *arguments)

    def tick(self, *, keep: bool = True) -> list[Difference]:
        return changes(GitClient(), self.hub, keep=keep)


class ConfigurationTests(ConfigurationCase):
    """The record of the hub's configuration that the shepherd's tick keeps, and what it reports against it."""

    def test_the_setting_is_the_one_the_guard_and_its_documents_name(self) -> None:
        # A pinned contract value: the opt-in "The guard" in docs/parallel-sessions.md tells a clone to set.
        self.assertEqual("coding-agent-skills.hubGuard", GUARD_SETTING)

    def test_a_first_tick_reports_nothing_and_records_the_configuration(self) -> None:
        self.configure("alias.sneak", "!true")
        self.assertEqual([], self.tick())
        self.assertTrue(self.record.is_file())
        self.assertEqual([], self.tick())

    def test_the_guard_turned_off_since_the_previous_tick_is_named_once(self) -> None:
        self.configure(GUARD_SETTING, "true")
        self.assertEqual([], self.tick())
        self.configure(GUARD_SETTING, "false")
        self.assertEqual(
            [Difference("unguarded", f"its {GUARD_SETTING} was true at the previous tick and is no longer")],
            self.tick(),
            "the guard's own setting is named by its own line, never as a changed setting too",
        )
        self.assertEqual([], self.tick())

    def test_the_guard_unset_or_set_to_what_git_cannot_read_as_a_boolean_is_off(self) -> None:
        for change in (("--unset", GUARD_SETTING), (GUARD_SETTING, "maybe")):
            with self.subTest(change=change):
                self.configure(GUARD_SETTING, "true")
                self.tick()
                self.configure(*change)
                self.assertEqual(["unguarded"], [difference.kind for difference in self.tick()])

    def test_a_setting_added_changed_or_removed_is_named_by_its_name_alone(self) -> None:
        self.configure("core.hooksPath", "hooks")
        self.configure("user.name", "someone")
        self.tick()
        self.configure("alias.sneak", "!secret value")
        self.configure("core.hooksPath", "elsewhere")
        self.configure("--unset", "user.name")
        self.assertEqual(
            [
                Difference(
                    "configured",
                    "its git configuration changed since the previous tick: alias.sneak added, core.hookspath "
                    "changed, user.name removed",
                )
            ],
            self.tick(),
        )
        self.assertNotIn("secret value", self.record.read_text(encoding="utf-8"), "the record keeps digests only")

    def test_a_value_added_to_a_setting_that_holds_several_changes_it(self) -> None:
        self.configure("--add", "remote.origin.fetch", "+refs/notes/*:refs/notes/*")
        self.tick()
        self.configure("--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*")
        self.assertEqual(["configured"], [difference.kind for difference in self.tick()])

    def test_past_the_named_settings_the_rest_are_counted(self) -> None:
        self.tick()
        for name in ("a", "b", "c", "d", "e"):
            self.configure(f"alias.{name}", "status")
        self.assertEqual(
            "its git configuration changed since the previous tick: alias.a added, alias.b added, alias.c added, and "
            "2 more",
            self.tick()[0].text,
        )

    def test_a_branch_s_settings_which_git_writes_for_each_worktree_branch_are_not_watched(self) -> None:
        self.tick()
        self.configure("branch.fix/v1.2.remote", "origin")
        self.configure("branch.fix/v1.2.merge", "refs/heads/fix/v1.2")
        self.assertEqual([], self.tick())
        self.configure("branch.autoSetupMerge", "always")
        self.assertEqual(
            [
                Difference(
                    "configured", "its git configuration changed since the previous tick: branch.autosetupmerge added"
                )
            ],
            self.tick(),
            "a branch setting with no branch named is the repository's own",
        )

    def test_without_keep_the_record_is_compared_and_left(self) -> None:
        self.tick()
        recorded = self.record.read_bytes()
        self.configure("alias.sneak", "!true")
        self.assertEqual(["configured"], [difference.kind for difference in self.tick(keep=False)])
        self.assertEqual(recorded, self.record.read_bytes())
        self.assertEqual(["configured"], [difference.kind for difference in self.tick()])
        self.assertEqual([], self.tick())

    def test_without_keep_and_no_record_nothing_is_written(self) -> None:
        self.assertEqual([], self.tick(keep=False))
        self.assertFalse(self.record.exists())

    def test_a_record_that_cannot_be_read_is_named_and_replaced(self) -> None:
        for content in ("not json", '{"version": 2, "guarded": true, "opted_in": true, "settings": {}}', "[]"):
            with self.subTest(content=content):
                self.record.write_text(content, encoding="utf-8")
                found = self.tick()
                self.assertEqual(["record"], [difference.kind for difference in found])
                self.assertTrue(
                    found[0].text.startswith(
                        f"its record of the previous tick, {self.record.as_posix()}, could not be read: "
                    ),
                    found[0].text,
                )
                self.assertEqual([], self.tick(), "the record was replaced by this tick's")

    def test_a_record_that_cannot_be_written_is_named(self) -> None:
        self.record.mkdir()
        found = self.tick()
        self.assertEqual(["record", "record"], [difference.kind for difference in found])
        self.assertIn("could not be written: ", found[1].text)

    def test_a_record_is_read_only_as_the_tick_writes_it(self) -> None:
        digest = "0" * 64
        cases = {
            "an unknown field": {"version": 1, "guarded": True, "opted_in": True, "settings": {}, "extra": 1},
            "a digest of another length": {"version": 1, "guarded": True, "opted_in": True, "settings": {"a": "0"}},
            "a guard that is no boolean": {"version": 1, "guarded": "yes", "opted_in": True, "settings": {}},
            "no opting in": {"version": 1, "guarded": True, "settings": {"a": digest}},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                json_writer.write(self.record, value)
                with self.assertRaises(RecordError):
                    read_record(self.record)


class LapseTests(ConfigurationCase):
    """The guard off in a clone whose record says it opted in, which repo-cleanup's report names."""

    def lapsed(self) -> Difference | None:
        return lapse(GitClient(), self.hub)

    def test_a_clone_no_tick_recorded_is_never_named(self) -> None:
        self.assertIsNone(self.lapsed())
        self.tick()
        self.assertIsNone(self.lapsed(), "a tick that never saw the guard on records no opting in")

    def test_the_guard_off_after_a_tick_saw_it_on_is_named_until_it_is_on_again(self) -> None:
        self.configure(GUARD_SETTING, "true")
        self.tick()
        self.assertIsNone(self.lapsed())
        self.configure(GUARD_SETTING, "false")
        expected = Difference(
            "lapsed", f"its {GUARD_SETTING} is not true, though a tick recorded that this clone opted in"
        )
        self.assertEqual(expected, self.lapsed())
        self.tick()
        self.assertEqual(expected, self.lapsed(), "a later tick that sees the guard off keeps the opting in")
        self.configure(GUARD_SETTING, "true")
        self.assertIsNone(self.lapsed())
        self.configure("--unset", GUARD_SETTING)
        self.record.unlink()
        self.assertIsNone(self.lapsed(), "deleting the record forgets the opting in")

    def test_a_record_that_cannot_be_read_is_named(self) -> None:
        self.record.write_text("not json", encoding="utf-8")
        self.assertEqual("record", getattr(self.lapsed(), "kind", None))

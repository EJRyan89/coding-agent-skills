"""Regression tests for the one check of how a main checkout differs from a clean one of its default branch at the
commit its remote's default branch holds."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hub_state
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
    escaped_name,
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
        return changes(GitClient(), self.hub, "main", keep=keep)


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

    def test_the_hub_s_own_branch_settings_are_watched_and_a_worktree_branch_s_are_not(self) -> None:
        self.tick()
        self.configure("branch.main.pushRemote", "elsewhere")
        self.configure("branch.fix/main.remote", "origin")
        self.assertEqual(
            [
                Difference(
                    "configured", "its git configuration changed since the previous tick: branch.main.pushremote added"
                )
            ],
            self.tick(),
        )

    def append_configuration(self, text: bytes) -> None:
        with (self.hub / ".git" / "config").open("ab") as configuration:
            configuration.write(text)

    def test_a_value_that_is_not_utf_8_is_digested_as_its_bytes(self) -> None:
        self.tick()
        self.append_configuration(b"[user]\n\tname = Jos\xe9\n")
        self.assertEqual(
            [Difference("configured", "its git configuration changed since the previous tick: user.name added")],
            self.tick(),
        )
        self.assertEqual([], self.tick(), "the record was written, so the change is named once")

    def test_a_name_given_alone_rewritten_with_an_empty_value_is_changed(self) -> None:
        self.append_configuration(b"[core]\n\tfsmonitor\n")
        self.tick()
        self.configure("core.fsmonitor", "")
        self.assertEqual(
            [Difference("configured", "its git configuration changed since the previous tick: core.fsmonitor changed")],
            self.tick(),
            "git reads a name alone as true and an empty value as false",
        )

    def test_a_name_that_is_not_utf_8_is_recorded_and_named_by_its_bytes(self) -> None:
        self.tick()
        self.append_configuration(b'[remote "caf\xe9"]\n\turl = elsewhere\n[remote "caf\\\\xe9"]\n\turl = other\n')
        self.assertEqual(
            [
                Difference(
                    "configured",
                    "its git configuration changed since the previous tick: remote.caf\\\\xe9.url added, "
                    "remote.caf\\xe9.url added",
                )
            ],
            self.tick(),
            "a byte and the text spelling it are told apart",
        )
        self.assertEqual([], self.tick(), "the record was written, so the change is named once")

    def test_a_repository_git_cannot_find_raises_naming_the_folder(self) -> None:
        outside = self.root / "not a repository"
        outside.mkdir()
        for check in (lambda: changes(GitClient(), outside, "main", keep=True), lambda: lapse(GitClient(), outside)):
            with self.subTest(check=check), self.assertRaises(GitError) as raised:
                check()
            self.assertEqual(f"{outside.as_posix()} is not in a git repository", str(raised.exception))

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

    def test_a_record_the_writer_refuses_is_named(self) -> None:
        with mock.patch.object(hub_state, "write_record", side_effect=ValueError("refused at the write")):
            found = self.tick()
        self.assertEqual(
            [
                Difference(
                    "record",
                    f"its record of the previous tick, {self.record.as_posix()}, could not be written: refused at the "
                    "write",
                )
            ],
            found,
        )

    def test_a_record_is_read_only_as_the_tick_writes_it(self) -> None:
        digest = "0" * 64
        cases = {
            "an unknown field": {"version": 1, "guarded": True, "opted_in": True, "settings": {}, "extra": 1},
            "a digest of another length": {"version": 1, "guarded": True, "opted_in": True, "settings": {"a": "0"}},
            "a guard that is no boolean": {"version": 1, "guarded": "yes", "opted_in": True, "settings": {}},
            "no opting in": {"version": 1, "guarded": True, "settings": {"a": digest}},
            "files that are no object": {"version": 1, "guarded": True, "opted_in": True, "settings": {}, "files": []},
            "a file's digest of another length": {
                "version": 1,
                "guarded": True,
                "opted_in": True,
                "settings": {},
                "files": {"hooks/pre-commit": "0"},
            },
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                json_writer.write(self.record, value)
                with self.assertRaises(RecordError):
                    read_record(self.record)


class FileTests(ConfigurationCase):
    """The record of the hub's hooks and info files that the shepherd's tick keeps, and what it reports against it."""

    def setUp(self) -> None:
        super().setUp()
        self.hooks = self.hub / ".git" / "hooks"
        self.hooks.mkdir(exist_ok=True)
        self.info = self.hub / ".git" / "info"
        self.info.mkdir(exist_ok=True)

    def hook(self, name: str, text: str, *, executable: bool = True) -> Path:
        path = self.hooks / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
        return path

    def files_changed(self, named: str) -> list[Difference]:
        return [Difference("hooks", f"its git hooks or info files changed since the previous tick: {named}")]

    def test_a_hook_added_changed_or_removed_is_named_once_by_its_name_alone(self) -> None:
        self.hook("pre-push", "#!/bin/sh\nexit 0\n")
        self.hook("post-merge", "#!/bin/sh\nexit 0\n")
        self.assertEqual([], self.tick())
        self.hook("pre-commit", "#!/bin/sh\necho secret hook\n")
        self.hook("pre-push", "#!/bin/sh\nexit 1\n")
        (self.hooks / "post-merge").unlink()
        self.assertEqual(
            self.files_changed("hooks/post-merge removed, hooks/pre-commit added, hooks/pre-push changed"), self.tick()
        )
        self.assertEqual([], self.tick(), "the record was written, so the change is named once")
        self.assertNotIn("secret hook", self.record.read_text(encoding="utf-8"), "the record keeps digests only")

    def test_git_s_samples_are_not_watched(self) -> None:
        self.tick()
        self.hook("pre-commit.sample", "#!/bin/sh\nexit 1\n")
        self.assertEqual([], self.tick(), "git never runs a hook by a .sample name")

    def test_each_info_file_git_reads_added_changed_or_removed_is_named(self) -> None:
        exclude, attributes = self.info / "exclude", self.info / "attributes"
        exclude.write_text("# git ls-files --others --exclude-from=.git/info/exclude\n", encoding="utf-8")
        attributes.unlink(missing_ok=True)
        self.tick()
        exclude.write_text("stray.txt\n", encoding="utf-8")
        attributes.write_text("* -diff\n", encoding="utf-8")
        self.assertEqual(self.files_changed("info/attributes added, info/exclude changed"), self.tick())
        exclude.unlink()
        self.assertEqual(self.files_changed("info/exclude removed"), self.tick())
        (self.info / "sparse-checkout").write_text("/*\n", encoding="utf-8")
        self.assertEqual([], self.tick(), "only the two info files every worktree's status reads are watched")

    def test_an_exclude_that_hides_an_untracked_hub_file_is_named_though_the_file_is_not(self) -> None:
        self.tick()
        (self.hub / "stray.txt").write_text("stray\n", encoding="utf-8")
        (self.info / "exclude").write_text("stray.txt\n", encoding="utf-8")
        self.assertEqual([], self.found())
        self.assertEqual(self.files_changed("info/exclude changed"), self.tick())

    def test_past_the_named_files_the_rest_are_counted(self) -> None:
        self.tick()
        for name in ("a", "b", "c", "d"):
            self.hook(name, name)
        self.assertEqual(self.files_changed("hooks/a added, hooks/b added, hooks/c added, and 1 more"), self.tick())

    def test_a_first_tick_or_one_after_a_record_that_holds_no_files_names_none_and_records_them(self) -> None:
        self.hook("pre-commit", "#!/bin/sh\n")
        self.assertEqual([], self.tick())
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertIn("hooks/pre-commit", recorded["files"])
        del recorded["files"]
        self.record.write_text(json.dumps(recorded), encoding="utf-8")
        self.hook("pre-commit", "#!/bin/sh\nexit 1\n")
        self.assertEqual([], self.tick(), "a record written before files were read has none to compare with")
        self.assertIn("files", json.loads(self.record.read_text(encoding="utf-8")))
        self.hook("pre-commit", "#!/bin/sh\nexit 2\n")
        self.assertEqual(self.files_changed("hooks/pre-commit changed"), self.tick())

    def test_without_keep_a_changed_file_is_named_and_the_record_left(self) -> None:
        self.tick()
        recorded = self.record.read_bytes()
        self.hook("pre-commit", "#!/bin/sh\n")
        for _ in range(2):
            self.assertEqual(self.files_changed("hooks/pre-commit added"), self.tick(keep=False))
        self.assertEqual(recorded, self.record.read_bytes())

    def test_a_folder_in_the_hooks_folder_is_watched_by_its_kind(self) -> None:
        self.tick()
        (self.hooks / "pre-commit").mkdir()
        self.assertEqual(self.files_changed("hooks/pre-commit added"), self.tick())
        (self.hooks / "pre-commit").rmdir()
        self.hook("pre-commit", "")
        self.assertEqual(self.files_changed("hooks/pre-commit changed"), self.tick())

    def test_a_hook_that_cannot_be_read_raises_with_the_reason_and_writes_no_record(self) -> None:
        self.hook("pre-commit", "#!/bin/sh\n")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")),
            self.assertRaises(GitError) as raised,
        ):
            self.tick()
        self.assertEqual("[Errno 13] Permission denied", str(raised.exception))
        self.assertFalse(self.record.exists())

    def test_a_hooks_folder_that_cannot_be_listed_raises_with_the_reason(self) -> None:
        with (
            mock.patch.object(os, "scandir", side_effect=PermissionError(13, "Permission denied")),
            self.assertRaises(GitError) as raised,
        ):
            self.tick()
        self.assertEqual("[Errno 13] Permission denied", str(raised.exception))

    def test_a_hooks_folder_that_is_not_there_holds_no_hooks(self) -> None:
        self.hook("pre-commit", "#!/bin/sh\n")
        self.tick()
        (self.hooks / "pre-commit").unlink()
        for sample in self.hooks.iterdir():
            sample.unlink()
        self.hooks.rmdir()
        self.assertEqual(self.files_changed("hooks/pre-commit removed"), self.tick())

    if sys.platform != "win32":  # Windows keeps no executable bit, and a link needs a privilege there

        def test_a_hook_made_executable_is_changed_and_an_info_file_s_mode_is_not(self) -> None:
            self.hook("pre-commit", "#!/bin/sh\n", executable=False)
            (self.info / "exclude").write_text("", encoding="utf-8")
            self.tick()
            (self.hooks / "pre-commit").chmod(0o755)
            (self.info / "exclude").chmod(0o755)
            self.assertEqual(
                self.files_changed("hooks/pre-commit changed"),
                self.tick(),
                "git runs a hook only when it is executable, and reads an info file whatever its mode",
            )
            (self.hooks / "pre-commit").chmod(0o744)
            self.assertEqual([], self.tick(), "only whether its owner may run it counts")

        def test_a_linked_hook_is_read_through_and_a_link_to_nothing_is_no_hook(self) -> None:
            target = self.root / "hook target"
            (self.hooks / "pre-commit").symlink_to(target)
            self.assertEqual([], self.tick())
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            self.assertEqual(self.files_changed("hooks/pre-commit added"), self.tick())
            target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            self.assertEqual(self.files_changed("hooks/pre-commit changed"), self.tick())

        def test_a_hook_name_that_is_not_utf_8_is_escaped_as_a_setting_s_is(self) -> None:
            self.assertEqual("pre\\xe9", escaped_name(os.fsencode(os.fsdecode(b"pre\xe9"))))
            self.assertEqual("a\\\\xe9", escaped_name(os.fsencode("a\\xe9")))

    if sys.platform.startswith("linux"):  # macOS and Windows refuse a file name that is not UTF-8

        def test_a_hook_whose_name_is_not_utf_8_is_recorded_and_named_by_its_bytes(self) -> None:
            self.tick()
            for name in (b"caf\xe9", b"caf\\xe9"):
                path = Path(os.fsdecode(bytes(self.hooks) + b"/" + name))
                path.write_bytes(b"#!/bin/sh\n")
            self.assertEqual(self.files_changed("hooks/caf\\\\xe9 added, hooks/caf\\xe9 added"), self.tick())
            self.assertEqual([], self.tick(), "the record was written, so the change is named once")


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

    def test_a_record_that_cannot_be_read_keeps_a_clone_that_opted_in_named(self) -> None:
        self.configure(GUARD_SETTING, "true")
        self.tick()
        self.record.write_text("not json", encoding="utf-8")
        self.configure(GUARD_SETTING, "false")
        self.assertEqual(["record"], [difference.kind for difference in self.tick()])
        self.assertEqual("lapsed", getattr(self.lapsed(), "kind", None), "an unknown opting in is read as one")

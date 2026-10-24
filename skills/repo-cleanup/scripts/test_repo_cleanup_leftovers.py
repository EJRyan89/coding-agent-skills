"""The leftovers of a cleanup: branches the default branch already holds, local-only and detached worktrees, and
directories Git no longer tracks, on the repositories test_repo_cleanup.py's Fixture builds."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import cleanup_policy
import repo_cleanup as rc
from test_repo_cleanup import (
    GIT_CONFIG,
    UNNAMEABLE,
    Fixture,
    build_template,
    facts,
    git,
    is_regenerable,
    refusing_branch_deletion,
)

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


def link_directory(target: Path, link: Path) -> None:
    """A link to a directory: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class LeftoverTests(Fixture):
    """Branches the default branch already holds, local-only and detached worktrees, and directories Git no longer
    tracks: each is cleaned when nothing is lost, offered when only the user may decide, or kept with the reason."""

    def landed(self, name: str) -> str:
        """A pushed branch that main holds through a merge, whose remote branch stays and that had no pull request."""
        tip = self.push_branch(name)
        self.merge_remotely(name)
        return tip

    def queried(self) -> set[str]:
        return {call[call.index("--head") + 1] for call in self.github.calls if call[:2] == ["pr", "list"]}

    def summary(self, lines: list[str]) -> str:
        return "\n".join(fields[0] for fields in facts(lines, "SUMMARY"))

    def offered(self, lines: list[str], kind: str) -> dict[str, str]:
        return {fields[0]: fields[1] for fields in facts(lines, kind)}

    def same(self, expected: Path, actual: str) -> None:
        self.assertTrue(rc.same_path(str(expected), actual), (expected, actual))

    def preserved(self, lines: list[str], *expected: tuple[Path, str]) -> None:
        """The PRESERVED lines name these paths, in Git's spelling or the one given, with these reasons."""
        actual = facts(lines, "PRESERVED")
        self.assertEqual([why for _, why in expected], [fields[1] for fields in actual], lines)
        for (path, _), fields in zip(expected, actual, strict=True):
            self.same(path, fields[0])

    def test_a_branch_main_holds_with_a_remote_and_no_pull_request_is_cleaned_as_merged(self) -> None:
        for name in ("landed", "landed-tree", "landed-dirty", "landed-locked", "landed-open"):
            self.landed(name)
        self.github.pull("landed-open", "OPEN", self.tip("landed-open") or "")
        self.landed("upstream-ahead")
        newer = self.advance_remotely("upstream-ahead")
        own = self.push_branch("own-work")
        git(self.clone, "fetch", "--quiet", "origin")
        git(self.clone, "branch", "--quiet", "--track", "started", "origin/main")
        trees = {name: self.area / f"{name} wt" for name in ("landed-tree", "landed-dirty", "landed-locked", "started")}
        for name, path in trees.items():
            git(self.clone, "worktree", "add", "--quiet", str(path), name)
        (trees["landed-dirty"] / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        git(self.clone, "worktree", "lock", str(trees["landed-locked"]))
        planned, applied = self.clean()
        actions = {fields[0]: (fields[1], fields[2], fields[4], fields[5]) for fields in facts(planned, "BRANCH")}
        self.assertEqual(
            {
                "landed": ("tracking", "NONE", "delete", "-"),
                "landed-tree": ("tracking", "NONE", "remove-worktree", "-"),
                "landed-dirty": ("tracking", "-", "keep", "worktree-dirty"),
                "landed-locked": ("tracking", "NONE", "remove-worktree", "-"),
                "landed-open": ("tracking", "OPEN", "keep", "pr-open"),
                "upstream-ahead": ("tracking", "-", "keep", "-"),
                "own-work": ("tracking", "-", "keep", "-"),
                "started": ("tracking", "NONE", "keep", "in-worktree"),
            },
            actions,
        )
        self.assertTrue(trees["started"].is_dir(), "a branch started from origin/main is not one main holds")
        self.assertEqual(
            {"landed", "landed-tree", "landed-locked", "landed-open", "started"},
            self.queried(),
            "a branch without a clean worktree is looked up only when the default branch holds it",
        )
        self.assertEqual([["upstream-ahead", newer]], facts(planned, "FASTFORWARD"))
        self.assertEqual({"landed", "landed-tree"}, {fields[0] for fields in facts(applied, "DELETED")})
        self.assertEqual(["landed-tree"], [fields[1] for fields in facts(applied, "REMOVED")])
        self.assertFalse(trees["landed-tree"].exists())
        locked = [why for name, why in facts(applied, "PRESERVED") if name == "landed-locked"]
        self.assertEqual(1, len(locked), applied)
        self.assertTrue(locked[0].startswith("Git refused to remove worktree "), locked)
        for name in ("landed-dirty", "landed-locked", "landed-open"):
            self.assertIsNotNone(self.tip(name), name)
        self.assertEqual((newer, own), (self.tip("upstream-ahead"), self.tip("own-work")))
        summary = self.summary(applied)
        self.assertIn("  Dirty worktrees skipped: 1 — ", summary)
        self.assertIn("  Open PR (kept):          1 — landed-open", summary)
        git(self.clone, "worktree", "unlock", str(trees["landed-locked"]))

    def test_a_local_only_branch_in_a_clean_worktree_is_offered_and_deleted_with_it_on_confirmation(self) -> None:
        git(self.clone, "switch", "--quiet", "-c", "work", "main")
        work = self.commit(self.clone, "unpublished work")
        git(self.clone, "switch", "--quiet", "main")
        for name in ("fresh", "drafted", "reviewing", "later"):
            git(self.clone, "branch", name, "main")
        self.github.pull("reviewing", "OPEN", self.tip("reviewing") or "")
        trees = {name: self.area / f"{name} wt" for name in ("fresh", "work", "drafted", "reviewing", "later")}
        for name, path in trees.items():
            git(self.clone, "worktree", "add", "--quiet", str(path), name)
        (trees["drafted"] / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        planned, applied = self.clean()
        self.assertEqual(
            {
                "fresh": ("NONE", "ask-remove-worktree"),
                "work": ("NONE", "ask-remove-worktree"),
                "later": ("NONE", "ask-remove-worktree"),
                "drafted": ("NONE", "keep"),
                "reviewing": ("OPEN", "keep"),
            },
            {fields[0]: (fields[2], fields[4]) for fields in facts(planned, "BRANCH")},
        )
        self.assertEqual(
            {"fresh wt": "ask", "work wt": "ask", "later wt": "ask", "drafted wt": "dirty:1", "reviewing wt": "keep"},
            {Path(fields[0]).name: fields[2] for fields in facts(planned, "WORKTREE")},
        )
        offered = self.offered(applied, "CONFIRM_LOCAL")
        self.assertEqual({"fresh", "work", "later"}, set(offered))
        for name, where in offered.items():
            self.same(trees[name], where)
        self.assertEqual([], facts(applied, "REMOVED"), "nothing is removed before the user answers")
        self.assertTrue(all(path.exists() for path in trees.values()))
        summary = self.summary(applied)
        self.assertIn("  Local-only (kept):       3 — ", summary)
        self.assertIn(f"work (in worktree {offered['work']})", summary)

        (trees["later"] / "notes.txt").write_text("started after the sweep\n", encoding="utf-8")
        code, lines = self.confirm("delete-local", "fresh", "work", "later", "reviewing")
        self.assertEqual(1, code, lines)
        self.assertEqual({"fresh", "work"}, {fields[1] for fields in facts(lines, "REMOVED")})
        self.assertEqual([["fresh"]], facts(lines, "DELETED"))
        self.assertEqual([["work", work]], facts(lines, "UNMERGED"))
        preserved = dict(facts(lines, "PRESERVED"))
        self.assertTrue(preserved["later"].startswith("Git refused to remove worktree "), preserved)
        self.assertEqual("not a local-only or closed branch offered by this plan", preserved["reviewing"])
        self.assertFalse(trees["fresh"].exists() or trees["work"].exists())
        self.assertTrue((trees["later"] / "notes.txt").exists())
        self.assertIsNone(self.tip("fresh"))
        self.assertIsNotNone(self.tip("later"))
        code, lines = self.confirm("force-delete", "work")
        self.assertEqual([["work"]], facts(lines, "DELETED"))
        self.assertIsNone(self.tip("work"))

    def test_a_branch_git_refuses_to_delete_after_its_worktree_is_removed_is_preserved_and_exits_1(self) -> None:
        trees = {name: self.area / f"{name} wt" for name in ("fine", "held", "beside")}
        for name, path in trees.items():
            git(self.clone, "branch", name, "main")
            git(self.clone, "worktree", "add", "--quiet", str(path), name)
        _, applied = self.clean()
        self.assertEqual({"fine", "held", "beside"}, set(self.offered(applied, "CONFIRM_LOCAL")))
        code, lines = self.confirm("delete-local", "fine")
        self.assertEqual(0, code, lines)
        self.assertEqual(
            ([["fine"]], ["fine"]), (facts(lines, "DELETED"), [fields[1] for fields in facts(lines, "REMOVED")])
        )

        output = io.StringIO()
        services = rc.Services(git=refusing_branch_deletion("held"), gh=self.github)
        with contextlib.redirect_stdout(output):
            code = rc.main(
                ["delete-local", "--plan", str(self.plan_file), "--branch", "held", "--branch", "beside"], services
            )
        lines = output.getvalue().splitlines()
        self.assertEqual(1, code, lines)
        self.assertEqual(["held", "beside"], [fields[1] for fields in facts(lines, "REMOVED")])
        self.assertEqual([["held", "error: cannot lock ref 'refs/heads/held'"]], facts(lines, "PRESERVED"))
        self.assertEqual([["beside"]], facts(lines, "DELETED"))
        self.assertFalse(trees["held"].exists() or trees["beside"].exists())
        self.assertIsNotNone(self.tip("held"))
        self.assertIsNone(self.tip("beside"))
        self.assertIn("held: error: cannot lock ref 'refs/heads/held'", self.summary(lines))

    def test_a_branch_tracking_a_remote_branch_of_another_name_is_kept_though_main_holds_it(self) -> None:
        for name in ("feature/nested", "feature/nested-tree", "exact"):
            self.landed(name)
        # A task started from origin/feature/<name> under the shorter name tracks the longer one.
        for name in ("nested", "nested-tree"):
            git(self.clone, "branch", "-m", f"feature/{name}", name)
        self.assertEqual(
            "refs/remotes/origin/feature/nested",
            git(self.clone, "rev-parse", "--symbolic-full-name", "nested@{upstream}"),
        )
        tree = self.area / "nested-tree wt"
        git(self.clone, "worktree", "add", "--quiet", str(tree), "nested-tree")
        planned, applied = self.clean()
        self.assertEqual(
            {
                "nested": ("-", "keep", "-"),
                "nested-tree": ("NONE", "keep", "in-worktree"),
                "exact": ("NONE", "delete", "-"),
            },
            {fields[0]: (fields[2], fields[4], fields[5]) for fields in facts(planned, "BRANCH")},
        )
        self.assertEqual([["exact"]], facts(applied, "DELETED"))
        self.assertEqual([], facts(applied, "REMOVED"))
        self.assertTrue(tree.is_dir())
        self.assertIsNotNone(self.tip("nested"))
        self.assertIsNotNone(self.tip("nested-tree"))

    def test_a_branch_tracking_another_remotes_branch_of_its_name_is_kept_and_a_nested_name_is_cleaned(self) -> None:
        for name in ("forked", "feature/x"):
            self.landed(name)
        git(self.clone, "remote", "add", "fork", str(self.remote))
        git(self.clone, "fetch", "--quiet", "fork")
        git(self.clone, "branch", "--quiet", "--set-upstream-to", "fork/forked", "forked")
        self.assertEqual(
            ("refs/remotes/fork/forked", "refs/remotes/origin/feature/x"),
            tuple(
                git(self.clone, "rev-parse", "--symbolic-full-name", f"{name}@{{upstream}}")
                for name in ("forked", "feature/x")
            ),
        )
        planned, applied = self.clean()
        self.assertEqual(
            {"forked": "keep", "feature/x": "delete"},
            {fields[0]: fields[4] for fields in facts(planned, "BRANCH")},
        )
        self.assertEqual([["feature/x"]], facts(applied, "DELETED"))
        self.assertIsNotNone(self.tip("forked"))
        self.assertIsNone(self.tip("feature/x"))

    def test_a_detached_worktree_is_offered_when_clean_and_on_a_remote_branch_else_kept_with_why(self) -> None:
        main = git(self.clone, "rev-parse", "main")
        pushed = self.push_branch("pushed")
        trees = {name: self.area / f"{name} wt" for name in ("on-main", "on-remote", "own-commit", "dirty")}
        for name, path in trees.items():
            git(
                self.clone, "worktree", "add", "--quiet", "--detach", str(path), pushed if name == "on-remote" else main
            )
        own = self.commit(trees["own-commit"], "detached work")
        (trees["dirty"] / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        protected = self.area / "release" / "detached wt"
        git(self.clone, "worktree", "add", "--quiet", "--detach", str(protected), main)
        planned, applied = self.clean()
        self.assertEqual(
            {
                "on-main wt": "ask",
                "on-remote wt": "ask",
                "own-commit wt": "keep",
                "dirty wt": "keep",
                "detached wt": "protected",
            },
            {Path(fields[0]).name: fields[2] for fields in facts(planned, "WORKTREE")},
        )
        offered = {Path(path).name: sha for path, sha in facts(applied, "CONFIRM_DETACHED")}
        self.assertEqual({"on-main wt": main, "on-remote wt": pushed}, offered)
        summary = self.summary(applied)
        self.assertIn("  Detached (kept):         4 — ", summary)
        self.assertIn(f"own-commit wt (its commit {own[:12]} is on neither main nor a remote branch)", summary)
        self.assertIn("dirty wt (1 uncommitted change)", summary)
        self.assertIn("on-main wt (clean, its commit is on main or a remote branch)", summary)
        self.assertIn("  Protected worktrees:     1 (preserved)", summary)
        self.assertTrue(all(path.exists() for path in trees.values()), "nothing is removed before the user answers")

        git(self.clone, "update-ref", "-d", "refs/remotes/origin/pushed")
        code, lines = self.invoke(
            "remove-directory",
            "--plan",
            str(self.plan_file),
            *("--path", str(trees["on-main"])),
            *("--path", str(trees["on-remote"])),
            *("--path", str(trees["own-commit"])),
        )
        self.assertEqual(1, code, "a path the plan never offered needs the agent")
        self.assertEqual(["detached"], [fields[1] for fields in facts(lines, "REMOVED")])
        self.same(trees["on-main"], facts(lines, "REMOVED")[0][0])
        self.assertEqual(
            [
                f"its commit {pushed[:12]} is no longer on main or a remote branch",
                "not a detached worktree or orphan directory offered by this plan",
            ],
            [why for _, why in facts(lines, "PRESERVED")],
        )
        self.assertFalse(trees["on-main"].exists())
        self.assertTrue(trees["on-remote"].exists() and trees["own-commit"].exists())
        self.assertIn(" (detached)", self.summary(lines))
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(trees["on-main"]))
        self.assertEqual(0, code, "a path the plan already removed needs nothing more")
        self.preserved(lines, (trees["on-main"], "already removed by this plan"))

    def test_a_worktree_or_branch_that_moves_after_planning_is_preserved_as_moved_and_never_offered(self) -> None:
        main = git(self.clone, "rev-parse", "main")
        trees = {name: self.area / f"{name} wt" for name in ("kept", "moving")}
        for path in trees.values():
            git(self.clone, "worktree", "add", "--quiet", "--detach", str(path), main)
        git(self.clone, "branch", "fresh", "main")
        git(self.clone, "branch", "in-tree", "main")
        git(self.clone, "worktree", "add", "--quiet", str(self.area / "in-tree wt"), "in-tree")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        _, planned = self.plan()
        self.assertEqual(
            {"fresh": "ask-delete", "in-tree": "ask-remove-worktree"},
            {fields[0]: fields[4] for fields in facts(planned, "BRANCH") if fields[0] in {"fresh", "in-tree"}},
        )
        self.assertEqual(
            {"kept wt": "ask", "moving wt": "ask"},
            {
                Path(fields[0]).name: fields[2]
                for fields in facts(planned, "WORKTREE")
                if Path(fields[0]).name in {"kept wt", "moving wt"}
            },
        )
        moved = self.commit(trees["moving"], "work started after the plan")
        fresh = self.commit_locally("fresh")
        in_tree = self.commit(self.area / "in-tree wt", "work started after the plan")
        _, applied = self.apply()
        self.assertEqual({"kept wt": main}, {Path(path).name: sha for path, sha in facts(applied, "CONFIRM_DETACHED")})
        self.assertEqual([], facts(applied, "CONFIRM_LOCAL"))
        preserved = {Path(fields[0]).name: fields[1] for fields in facts(applied, "PRESERVED")}
        self.assertEqual({"fresh": "moved", "in-tree": "moved", "moving wt": "moved"}, preserved)
        self.assertEqual((fresh, in_tree), (self.tip("fresh"), self.tip("in-tree")))
        self.assertEqual(moved, git(trees["moving"], "rev-parse", "HEAD"))

        self.commit(trees["kept"], "work started after the offer")
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(trees["kept"]))
        self.assertEqual(0, code, lines)
        self.preserved(lines, (trees["kept"], "moved"))
        self.assertEqual([], facts(lines, "REMOVED"))
        self.assertTrue(trees["kept"].is_dir())

    def test_directories_git_no_longer_tracks_are_reported_and_deleted_only_on_confirmation(self) -> None:
        self.push_branch("live")
        self.push_branch("grouped")
        git(self.clone, "worktree", "add", "--quiet", str(self.area / "live wt"), "live")
        git(self.clone, "worktree", "add", "--quiet", str(self.area / "group" / "grouped wt"), "grouped")
        leftover = self.area / "group" / "leftover"
        leftover.mkdir()
        (leftover / "file.txt").write_text("left\n", encoding="utf-8")
        # As a removal on Windows leaves it: Git deregistered the worktree and deleted its .git file, but a locked
        # file kept the directory, and the tool that removed it left a marker to finish the cleanup later.
        stale = self.area / "stale wt"
        (stale / "src").mkdir(parents=True)
        (stale / "src" / "locked.txt").write_text("in use\n", encoding="utf-8")
        (stale / "src" / "locked.txt").chmod(stat.S_IREAD)
        (stale / ".pending-removal").write_text("", encoding="utf-8")
        outside = self.root / "outside target"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        (outside / "inner").mkdir()
        link_directory(outside, stale / "link")
        link_directory(outside, self.area / "linked")
        # Inside a directory the search enters because it holds a worktree.
        link_directory(outside, self.area / "group" / "linked")
        pruned = self.area / "pruned wt"
        pruned.mkdir()
        (pruned / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        task = self.clone / ".claude" / "worktrees" / "old task"
        task.mkdir(parents=True)
        (task / "notes.md").write_text("notes\n", encoding="utf-8")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
        _, applied = self.clean()
        orphans = [fields[0] for fields in facts(applied, "ORPHAN")]
        self.assertEqual(3, len(orphans), applied)
        for expected, actual in zip((leftover, stale, task), orphans, strict=True):
            self.same(expected, actual)
        self.assertIn("  Orphans (kept):          3 — ", self.summary(applied))
        self.assertTrue(leftover.exists() and stale.exists() and task.exists(), "nothing is deleted unasked")

        code, lines = self.invoke(
            "remove-directory", "--plan", str(self.plan_file), "--path", str(stale), "--path", str(leftover)
        )
        self.assertEqual(0, code, lines)
        self.assertEqual(2, len(facts(lines, "DELETED_DIR")), lines)
        self.assertFalse(os.path.lexists(stale) or os.path.lexists(leftover))
        self.assertTrue((outside / "keep.txt").exists(), "a link inside an orphan is removed, never followed")
        self.assertTrue((outside / "inner").is_dir())
        self.assertTrue(os.path.lexists(self.area / "linked") and (self.area / "group" / "grouped wt").is_dir())
        self.assertTrue(pruned.is_dir() and (self.area / "live wt").is_dir())
        self.assertEqual([], facts(lines, "PRUNED_DIR"), "the group still holds a worktree")
        summary = self.summary(lines)
        self.assertIn("  Orphans deleted:         2 — ", summary)
        self.assertIn("  Orphans (kept):          1 — ", summary)

        (task / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        code, lines = self.invoke(
            "remove-directory",
            "--plan",
            str(self.plan_file),
            *("--path", str(task)),
            *("--path", str(self.area / "live wt")),
        )
        self.assertEqual(1, code, "a path the plan never offered needs the agent")
        self.preserved(
            lines,
            (task, "no longer a directory Git does not track"),
            (self.area / "live wt", "not a detached worktree or orphan directory offered by this plan"),
        )
        self.assertTrue((task / "notes.md").exists())

    def tracked_reason(self, why: str, holder: Path, *files: str) -> None:
        """The PRESERVED reason says Git tracks `files` in the directory, in the repository at `holder`."""
        prefix, listed = (
            f"Git tracks {len(files)} file{'' if len(files) == 1 else 's'} in it, in ",
            f": {', '.join(files)}",
        )
        self.assertTrue(why.startswith(prefix) and why.endswith(listed), why)
        self.same(holder, why.removeprefix(prefix).removesuffix(listed))

    def test_a_folder_git_tracks_in_the_worktree_area_is_kept_with_the_reason_and_never_deleted(self) -> None:
        templates = self.clone / ".claude" / "worktrees" / "templates"
        templates.mkdir(parents=True)
        (templates / "base.md").write_text("tracked\n", encoding="utf-8")
        git(self.clone, "add", ".claude/worktrees/templates/base.md")
        self.commit(self.clone, "track a template")
        git(self.clone, "push", "--quiet", "origin", "main")
        (templates / "local.env").write_text("ignored\n", encoding="utf-8")
        task = self.clone / ".claude" / "worktrees" / "old task"
        task.mkdir()
        (task / "notes.md").write_text("notes\n", encoding="utf-8")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "exclude").write_text("*.env\n.claude/worktrees/old task/\n", encoding="utf-8")
        _, applied = self.clean()
        orphans = facts(applied, "ORPHAN")
        self.assertEqual(1, len(orphans), applied)
        self.same(task, orphans[0][0])
        (kept, why), *rest = facts(applied, "PRESERVED")
        self.assertEqual([], rest, applied)
        self.same(templates, kept)
        self.tracked_reason(why, self.clone, ".claude/worktrees/templates/base.md")
        self.assertIn("  Preserved:               1 — ", self.summary(applied))

        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(templates))
        self.assertEqual(1, code, "a path the plan never offered needs the agent")
        self.preserved(lines, (templates, "not a detached worktree or orphan directory offered by this plan"))
        self.assertTrue((templates / "base.md").exists() and (templates / "local.env").exists())
        self.assertEqual("", git(self.clone, "status", "--porcelain"), "the main worktree lost no tracked file")

        # Git starts tracking the offered directory after the sweep: the confirmed deletion asks again.
        git(self.clone, "add", "--force", ".claude/worktrees/old task/notes.md")
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(task))
        self.assertEqual(1, code, lines)
        (kept, why), *rest = facts(lines, "PRESERVED")
        self.assertEqual([], rest, lines)
        self.same(task, kept)
        self.tracked_reason(why, self.clone, ".claude/worktrees/old task/notes.md")
        self.assertEqual([], facts(lines, "DELETED_DIR"))
        self.assertTrue((task / "notes.md").exists())

    def test_a_repository_named_like_the_shared_worktree_area_keeps_its_tracked_folders(self) -> None:
        holder = self.repos / "Worktrees"
        notes = self.area / "notes"
        notes.mkdir(parents=True)
        (notes / "readme.md").write_text("tracked by the Worktrees repository\n", encoding="utf-8")
        git(holder, "init", "--quiet")
        git(holder, "add", "my repo/notes/readme.md")
        self.commit(holder, "track notes")
        leftover = self.area / "leftover"
        leftover.mkdir()
        (leftover / "file.txt").write_text("left\n", encoding="utf-8")
        _, applied = self.clean()
        orphans = facts(applied, "ORPHAN")
        self.assertEqual(1, len(orphans), applied)
        self.same(leftover, orphans[0][0])
        (kept, why), *rest = facts(applied, "PRESERVED")
        self.assertEqual([], rest, applied)
        self.same(notes, kept)
        self.tracked_reason(why, holder, "my repo/notes/readme.md")
        self.assertTrue((notes / "readme.md").exists())

    def test_a_directory_is_kept_when_git_cannot_say_whether_a_repository_holding_it_tracks_files_in_it(self) -> None:
        holder = self.repos / "Worktrees"
        stale = self.area / "stale wt"
        stale.mkdir(parents=True)
        (holder / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        _, applied = self.clean()
        self.assertEqual([], facts(applied, "ORPHAN"), applied)
        (kept, why), *rest = facts(applied, "PRESERVED")
        self.assertEqual([], rest, applied)
        self.same(stale, kept)
        self.assertTrue(why.startswith(f"could not ask Git whether {holder.as_posix()} tracks files in it: "), why)
        self.assertTrue(stale.is_dir())

    def outside_area(self, name: str, *inner: str) -> Path:
        """A directory outside the repository and the worktree area, holding `inner` and a victim directory in it."""
        target = self.root / name
        victim = target.joinpath(*inner, "victim")
        victim.mkdir(parents=True)
        (victim / "keep.txt").write_text("keep\n", encoding="utf-8")
        return target

    def test_a_link_on_the_way_to_a_worktree_area_is_never_followed_and_nothing_beyond_it_is_offered(self) -> None:
        # A junction on Windows and a symbolic link elsewhere: the repository's .claude, and the shared area itself.
        claude = self.outside_area("claude target", "worktrees")
        link_directory(claude, self.clone / ".claude")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        # No trailing slash: Git lists a symbolic link as a file, which a directory-only pattern does not exclude.
        (self.clone / ".git" / "info" / "exclude").write_text(".claude\n", encoding="utf-8")
        shared = self.outside_area("shared target", "my repo")
        link_directory(shared, self.repos / "Worktrees")
        _, applied = self.clean()
        self.assertEqual([], facts(applied, "ORPHAN"))
        plan = rc.load_plan(self.plan_file)
        self.assertEqual([], rc.orphan_directories([], plan["orphan_areas"], rc.area_anchors(plan)))

        # Were a search to name a directory beyond the link, the confirmed deletion still refuses it.
        victim = self.clone / ".claude" / "worktrees" / "victim"
        output = io.StringIO()
        with (
            mock.patch.object(rc, "orphan_directories", return_value=[str(victim)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertFalse(rc.delete_orphan(rc.Services(gh=self.github), plan, str(victim)))
        self.assertEqual(
            [[str(victim), "it resolves outside the worktree areas"]],
            facts(output.getvalue().splitlines(), "PRESERVED"),
        )
        self.assertTrue((claude / "worktrees" / "victim" / "keep.txt").exists())
        self.assertTrue((shared / "my repo" / "victim" / "keep.txt").exists())

    def test_a_worktree_area_that_is_itself_a_link_is_never_searched(self) -> None:
        # A junction on Windows and a symbolic link elsewhere, in place of the repository's .claude/worktrees.
        target = self.outside_area("worktrees target")
        (self.clone / ".claude").mkdir()
        link_directory(target, self.clone / ".claude" / "worktrees")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "exclude").write_text(".claude\n", encoding="utf-8")
        _, applied = self.clean()
        self.assertEqual([], facts(applied, "ORPHAN"))
        plan = rc.load_plan(self.plan_file)
        self.assertEqual([], rc.orphan_directories([], plan["orphan_areas"], rc.area_anchors(plan)))
        self.assertTrue((target / "victim" / "keep.txt").exists())

    def test_the_confirmed_deletion_refuses_a_directory_that_resolves_outside_its_worktree_area(self) -> None:
        stale = self.area / "stale wt"
        stale.mkdir(parents=True)
        _, applied = self.clean()
        self.assertEqual(1, len(facts(applied, "ORPHAN")), applied)
        target = self.outside_area("outside target")
        link_directory(target, self.area / "linked")
        # The area is searchable, but the directory named lies beyond a link inside it.
        beyond = self.area / "linked" / "victim"
        plan = rc.load_plan(self.plan_file)
        output = io.StringIO()
        with (
            mock.patch.object(rc, "orphan_directories", return_value=[str(beyond)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertFalse(rc.delete_orphan(rc.Services(gh=self.github), plan, str(beyond)))
        self.assertEqual(
            [[str(beyond), "it resolves outside the worktree areas"]],
            facts(output.getvalue().splitlines(), "PRESERVED"),
        )
        self.assertTrue((target / "victim" / "keep.txt").exists())

    def test_an_orphan_that_cannot_be_deleted_is_kept_with_the_reason_and_exits_1(self) -> None:
        stale = self.area / "stale wt"
        stale.mkdir(parents=True)
        (stale / "locked.txt").write_text("in use\n", encoding="utf-8")
        _, applied = self.clean()
        self.assertEqual(1, len(facts(applied, "ORPHAN")), applied)
        with mock.patch.object(shutil, "rmtree", side_effect=PermissionError(13, "Permission denied")):
            code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(stale))
        self.assertEqual(1, code, lines)
        self.preserved(lines, (stale, "could not delete it: [Errno 13] Permission denied"))
        self.assertTrue((stale / "locked.txt").exists())
        self.assertIn("  Orphans (kept):          1 — ", self.summary(lines))

    def worktree_facts(self, planned: list[str]) -> dict[str, tuple[str, str]]:
        """Each worktree the plan judged, by folder name: its action and detail."""
        return {Path(fields[0]).name: (fields[2], fields[3]) for fields in facts(planned, "WORKTREE")}

    def test_a_worktree_with_an_operation_in_progress_or_refs_of_its_own_is_kept_with_the_reason(self) -> None:
        for step in range(3):
            self.commit(self.clone, f"step {step}")
        git(self.clone, "push", "--quiet", "origin", "main")
        main = git(self.clone, "rev-parse", "main")
        trees = {name: self.area / f"{name} wt" for name in ("bisecting", "rebasing", "ref-holding", "plain")}
        for name in ("bisecting", "ref-holding", "plain"):
            git(self.clone, "worktree", "add", "--quiet", "--detach", str(trees[name]), main)
        git(trees["bisecting"], "bisect", "start", "main", "main~2")
        git(self.clone, "branch", "rebasing", "main~1")
        git(self.clone, "worktree", "add", "--quiet", str(trees["rebasing"]), "rebasing")
        stopped = subprocess.run(
            ["git", "-C", str(trees["rebasing"]), "rebase", "--exec", "exit 1", "main~2"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, stopped.returncode, "the rebase stops at its failing exec line")
        own = git(trees["ref-holding"], "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "kept by a worktree ref")
        git(trees["ref-holding"], "update-ref", "refs/worktree/keep", own)
        for tree in trees.values():
            self.assertEqual("", git(tree, "status", "--porcelain"), tree)
        planned, applied = self.clean()
        judged = self.worktree_facts(planned)
        self.assertEqual(("ask", "clean, its commit is on main or a remote branch"), judged["plain wt"])
        action, detail = judged["bisecting wt"]
        self.assertEqual("keep", action)
        self.assertTrue(
            detail.startswith(
                "has a bisect in progress; holds 2 refs only it sees: refs/bisect/bad, refs/bisect/good-"
            ),
            detail,
        )
        self.assertEqual(("keep", "has a rebase in progress"), judged["rebasing wt"])
        self.assertEqual(("keep", "holds 1 ref only it sees: refs/worktree/keep"), judged["ref-holding wt"])
        self.assertEqual(["plain wt"], [Path(fields[0]).name for fields in facts(applied, "CONFIRM_DETACHED")])
        self.assertIn("rebasing wt (has a rebase in progress)", self.summary(applied))

        # A ref made after the offer: the confirmed removal asks again and keeps the worktree.
        git(trees["plain"], "update-ref", "refs/worktree/later", own)
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(trees["plain"]))
        self.assertEqual(1, code, lines)
        (kept, why), *rest = facts(lines, "PRESERVED")
        self.assertEqual([], rest, lines)
        self.same(trees["plain"], kept)
        self.assertTrue(why.endswith(" holds 1 ref only it sees: refs/worktree/later"), why)
        self.assertEqual([], facts(lines, "REMOVED"))
        self.assertTrue(all(tree.is_dir() for tree in trees.values()), "nothing is removed")
        self.assertEqual(own, git(trees["plain"], "rev-parse", "refs/worktree/later"))

    def test_edits_hidden_from_status_keep_a_worktree_and_a_sparse_worktree_is_still_removed(self) -> None:
        for name, content in (("settings.json", "{}\n"), ("docs/guide.md", "guide\n"), ("src/app.py", "pass\n")):
            (self.clone / name).parent.mkdir(parents=True, exist_ok=True)
            (self.clone / name).write_text(content, encoding="utf-8")
        git(self.clone, "add", "settings.json", "docs/guide.md", "src/app.py")
        self.commit(self.clone, "files")
        git(self.clone, "push", "--quiet", "origin", "main")
        for name in ("skipped", "assumed", "sparse"):
            self.finished_branch(name)
        trees = {name: self.area / f"{name} wt" for name in ("skipped", "assumed", "sparse", "detached")}
        for name in ("skipped", "assumed", "sparse"):
            git(self.clone, "worktree", "add", "--quiet", str(trees[name]), name)
        git(self.clone, "worktree", "add", "--quiet", "--detach", str(trees["detached"]), "main")
        for name, bit in (
            ("skipped", "--skip-worktree"),
            ("assumed", "--assume-unchanged"),
            ("detached", "--assume-unchanged"),
        ):
            (trees[name] / "settings.json").write_text('{"local": true}\n', encoding="utf-8")
            git(trees[name], "update-index", bit, "settings.json")
        git(trees["sparse"], "sparse-checkout", "set", "src")
        self.assertFalse((trees["sparse"] / "docs" / "guide.md").exists(), "the sparse checkout leaves docs out")
        for tree in trees.values():
            self.assertEqual("", git(tree, "status", "--porcelain"), tree)
        planned, applied = self.clean()
        hidden = "holds edits status does not show, to 1 file marked skip-worktree or assume-unchanged: settings.json"
        judged = self.worktree_facts(planned)
        self.assertEqual(
            {"skipped wt": ("held", hidden), "assumed wt": ("held", hidden)},
            {name: judged[name] for name in ("skipped wt", "assumed wt")},
        )
        self.assertEqual(("keep", hidden), judged["detached wt"])
        self.assertEqual(["sparse wt"], [Path(fields[0]).name for fields in facts(applied, "REMOVED")])
        self.assertEqual(["sparse"], [fields[0] for fields in facts(applied, "DELETED")])
        self.assertEqual([], facts(applied, "CONFIRM_DETACHED"))
        for name in ("skipped", "assumed", "detached"):
            self.assertEqual('{"local": true}\n', (trees[name] / "settings.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(self.tip("skipped"))
        self.assertIsNotNone(self.tip("assumed"))
        summary = self.summary(applied)
        self.assertIn("  Dirty worktrees skipped: 2 — ", summary)
        self.assertIn(f"(skipped, {hidden})", summary)

    def test_a_confirmed_orphan_whose_area_a_link_now_reaches_is_refused_with_the_reason_and_exits_1(self) -> None:
        stale = self.area / "stale wt"
        stale.mkdir(parents=True)
        (stale / "file.txt").write_text("left\n", encoding="utf-8")
        _, applied = self.clean()
        self.assertEqual(1, len(facts(applied, "ORPHAN")), applied)
        moved = self.root / "moved area"
        self.area.rename(moved)
        link_directory(moved, self.area)
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(stale))
        self.assertEqual(1, code, lines)
        self.preserved(lines, (stale, "it resolves outside the worktree areas"))
        self.assertTrue((moved / "stale wt" / "file.txt").is_file())

    def test_an_entry_whose_name_does_not_survive_one_line_is_not_offered_and_says_why(self) -> None:
        separator = "\u2028"
        branch = f"odd{separator}gone"
        self.finished_branch(branch, merged=False, state=None)
        sha = self.tip(branch)
        detached = self.area / f"odd{separator}detached wt"
        git(self.clone, "worktree", "add", "--quiet", "--detach", str(detached), "main")
        leftover = self.area / f"left{separator}over"
        leftover.mkdir()
        _, applied = self.clean()
        for kind in ("UNMERGED", "CONFIRM_DETACHED", "ORPHAN", "CONFIRM_LOCAL"):
            self.assertEqual([], facts(applied, kind), kind)
        offers = [fields for fields in facts(applied, "PRESERVED") if fields[1].startswith("not offered: ")]
        self.assertEqual([UNNAMEABLE.format(name) for name, _ in offers], [why for _, why in offers], applied)
        self.assertEqual(
            ["left over", "odd detached wt", "odd gone"], sorted(name.rsplit("/", 1)[-1] for name, _ in offers)
        )
        self.assertTrue(all(separator not in line for line in applied), "every fact stays on one line")
        self.assertEqual(sha, self.tip(branch))
        self.assertTrue(detached.is_dir() and leftover.is_dir())

    def test_a_link_where_a_regenerable_directory_is_named_is_not_regenerable(self) -> None:
        entries = cleanup_policy.configured(".venv")
        tree = self.root / "layout"
        (tree / "web").mkdir(parents=True)
        git(tree, "init", "--quiet")  # an added entry is asked of Git, which tracks nothing here
        (tree / "web" / "package.json").write_text("{}\n", encoding="utf-8")
        target = self.outside_area("store")
        link_directory(target, tree / "web" / "node_modules")
        link_directory(target, tree / ".vs")
        link_directory(target, tree / ".venv")
        for entry in ("web/node_modules/", "web/node_modules/victim/keep.txt", ".vs/", ".vs/victim/", ".venv/lib/"):
            with self.subTest(entry=entry):
                self.assertFalse(is_regenerable(entry, tree, entries))
        (tree / "real").mkdir()
        link_directory(tree / "real", tree / "linked")
        (tree / "real" / "__pycache__").mkdir()
        (tree / "real" / ".venv").mkdir()
        self.assertTrue(is_regenerable("real/__pycache__/", tree, entries))
        self.assertTrue(is_regenerable("real/.venv/", tree, entries))
        self.assertFalse(is_regenerable("linked/__pycache__/", tree, entries), "reached through a link")
        self.assertFalse(is_regenerable("linked/.venv/", tree, entries), "an addition, through a link")


def test_a_hidden_file_is_compared_only_when_its_index_mode_and_its_worktree_entry_are_both_regular(
    tmp_path: Path,
) -> None:
    # Every hash matches its index entry, so only an entry left uncompared is reported as edited.
    (tmp_path / "plain").write_text("x", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "linked").write_text("x", encoding="utf-8")
    (tmp_path / "linked-folder").mkdir()
    listing = [
        "S 100644 1111 0\tplain",
        "S 100755 2222 0\tfolder",
        "S 120000 3333 0\tlinked",
        "S 160000 4444 0\tlinked-folder",
    ]
    hashed: list[list[str]] = []

    def git_output(services: Any, path: str, *arguments: str) -> str:
        if arguments[0] == "ls-files":
            return "\0".join(listing)
        names = list(arguments[arguments.index("--") + 1 :])
        hashed.append(names)
        return "\n".join(
            {"plain": "1111", "folder": "2222", "linked": "3333", "linked-folder": "4444"}[n] for n in names
        )

    with mock.patch.object(rc, "git_output", side_effect=git_output):
        edited = rc.hidden_edits(rc.Services(), str(tmp_path))
    assert edited == ["folder", "linked", "linked-folder"]
    assert hashed == [["plain"]]

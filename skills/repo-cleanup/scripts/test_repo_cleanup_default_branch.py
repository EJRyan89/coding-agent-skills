"""The default branch a sweep works against, and the git lookups it rests on: each tells what git reports as absent
from a failure git names, so a ref, a HEAD, or an origin git cannot read is never taken for one that is not there."""

from __future__ import annotations

import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import git_client
import repo_cleanup as rc
from git_client import GitClient, GitResult
from test_repo_cleanup import GIT_CONFIG, CleanupTestCase, bare_git, git, remove_tree

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


class DefaultBranchTests(CleanupTestCase):
    """The branch origin/HEAD names, else origin/main, else origin/master."""

    def repository(self, branch: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup default ")).resolve()
        self.addCleanup(remove_tree, root)
        git(root, "init", "--quiet", "-b", branch)
        git(root, "commit", "--quiet", "--allow-empty", "-m", "initial")
        return root

    def test_origin_head_names_the_default_branch(self) -> None:
        root = self.repository("trunk")
        git(root, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.assertEqual(rc.DefaultBranch("trunk"), rc.default_branch(rc.Services(), root))

    def test_without_origin_head_main_then_master(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                root = self.repository("topic")
                git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
                self.assertEqual(rc.DefaultBranch(branch), rc.default_branch(rc.Services(), root))

    def test_a_repository_git_cannot_read_is_named_not_taken_for_one_without_a_default_branch(self) -> None:
        root = self.repository("topic")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        (root / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rc.CleanupError, f"^git symbolic-ref failed in {re.escape(str(root))}: .*bad config line 1"
        ):
            rc.default_branch(rc.Services(), root)

    def test_a_broken_ref_is_named_not_taken_for_no_commit(self) -> None:
        root = self.repository("topic")
        broken = root / ".git" / "refs" / "heads" / "broken"
        broken.write_text("nonsense\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rc.CleanupError, f"^git rev-parse failed in {re.escape(str(root))}: .*ignoring broken ref refs/heads/broken"
        ):
            rc.branch_tip(rc.Services(), root, "broken")
        self.assertIsNone(rc.branch_tip(rc.Services(), root, "missing"))
        self.assertEqual(git(root, "rev-parse", "HEAD"), rc.branch_tip(rc.Services(), root, "topic"))

    def test_origin_is_read_only_from_gits_answer_and_a_failure_is_named(self) -> None:
        root = self.repository("topic")
        self.assertIsNone(rc.name_with_owner(rc.Services(), root), "no origin URL")
        git(root, "remote", "add", "origin", "https://github.com/Owner/Name.git")
        self.assertEqual("Owner/Name", rc.name_with_owner(rc.Services(), root))
        git(root, "remote", "set-url", "origin", "https://example.invalid/owner/name.git")
        self.assertIsNone(rc.name_with_owner(rc.Services(), root), "a URL that names no GitHub repository")
        git(root, "config", "url.https://github.com/Owner/.insteadOf", "https://example.invalid/owner/")
        self.assertEqual("Owner/name", rc.name_with_owner(rc.Services(), root), "through insteadOf")
        (root / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rc.CleanupError, f"^git config failed in {re.escape(str(root))}: .*bad config line 1"
        ):
            rc.name_with_owner(rc.Services(), root)

    def test_a_containment_git_cannot_test_is_named_not_taken_for_not_contained(self) -> None:
        root = self.repository("topic")
        head = git(root, "rev-parse", "HEAD")
        self.assertTrue(rc.reachable(rc.Services(), root, head, "refs/heads/topic"))
        git(root, "commit", "--quiet", "--allow-empty", "-m", "later")
        later = git(root, "rev-parse", "HEAD")
        git(root, "branch", "--force", "old", head)
        self.assertFalse(rc.reachable(rc.Services(), root, later, "refs/heads/old"))
        (root / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rc.CleanupError, f"^git for-each-ref failed in {re.escape(str(root))}: .*bad config line 1"
        ):
            rc.reachable(rc.Services(), root, head, "refs/heads/topic")

    def test_a_worktree_head_git_cannot_read_is_named_not_taken_for_another_branch(self) -> None:
        root = self.repository("topic")
        real = rc.Services().git.runner

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if "symbolic-ref" in command:
                return GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")
            return real(command, timeout)

        services = rc.Services(git=GitClient(run))
        head = git(root, "rev-parse", "HEAD")
        found = rc.fast_forward(services, str(root), "topic", head, head, str(root))
        self.assertRegex(found, f"^git symbolic-ref failed in {re.escape(str(root))}: .*bad config line 1")

    def test_a_sync_whose_head_git_cannot_read_fails_naming_git_instead_of_switching(self) -> None:
        root = self.repository("topic")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        real = rc.Services().git.runner
        commands: list[list[str]] = []

        def run(command: Sequence[str], timeout: float) -> GitResult:
            commands.append(list(command))
            if "symbolic-ref" in command and command[-1] == "HEAD":
                return GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")
            return real(command, timeout)

        with self.assertRaisesRegex(
            rc.CleanupError, f"^git symbolic-ref failed in {re.escape(str(root))}: .*bad config line 1"
        ):
            rc.sync(str(root), False, rc.Services(git=GitClient(run)))
        self.assertFalse(any("switch" in command for command in commands), commands)

    def test_origin_has_only_the_branch_its_full_name_names(self) -> None:
        # Without --verify, show-ref reads the name as a pattern, which a tag ending in it matches.
        root = self.repository("topic")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(root, "update-ref", "refs/tags/refs/remotes/origin/gone", "HEAD")
        self.assertTrue(rc.origin_has(rc.Services(), root, "main"))
        self.assertFalse(rc.origin_has(rc.Services(), root, "gone"))

    def test_origin_head_names_only_a_branch_of_origin(self) -> None:
        root = self.repository("topic")
        self.assertIsNone(rc.origin_head(rc.Services(), root), "unset")
        git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/topic")
        self.assertIsNone(rc.origin_head(rc.Services(), root), "a branch outside origin")
        git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.assertEqual("trunk", rc.origin_head(rc.Services(), root))

    def test_the_current_branch_or_a_detached_head_is_named(self) -> None:
        root = self.repository("topic")
        self.assertEqual("topic", rc.current_branch(rc.Services(), root))
        git(root, "switch", "--quiet", "--detach")
        self.assertEqual("a detached HEAD", rc.current_branch(rc.Services(), root))

    def test_containment_asks_git_for_the_first_ref_that_holds_the_commit(self) -> None:
        commands: list[list[str]] = []

        def run(command: Sequence[str], timeout: float) -> GitResult:
            commands.append(list(command))
            return GitResult(0, "refs/heads/main\n", "")

        services = rc.Services(git=GitClient(run))
        self.assertTrue(rc.reachable(services, "root", "c" * 40, "refs/heads/main", "refs/remotes"))
        self.assertEqual(
            [
                [
                    *commands[0][:3],
                    "--no-optional-locks",
                    "for-each-ref",
                    "--count=1",
                    f"--contains={'c' * 40}",
                    "--format=%(refname)",
                    "refs/heads/main",
                    "refs/remotes",
                ]
            ],
            commands,
        )

    def test_a_worktree_whose_head_is_detached_no_longer_has_the_branch(self) -> None:
        root = self.repository("topic")
        head = git(root, "rev-parse", "HEAD")
        git(root, "switch", "--quiet", "--detach")
        found = rc.fast_forward(rc.Services(), str(root), "topic", head, head, str(root))
        self.assertEqual("the worktree no longer has the branch checked out", found)

    def test_a_sync_from_a_detached_head_switches_to_the_default_branch(self) -> None:
        root = self.repository("main")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(root, "switch", "--quiet", "--detach")
        lines: list[str] = []
        rc._capture.lines = lines
        try:
            rc.sync(str(root), False, rc.Services())
        finally:
            rc._capture.lines = None
        self.assertIn("CHECKOUT\tswitched", lines)
        self.assertEqual("refs/heads/main", git(root, "symbolic-ref", "HEAD"))

    def test_git_that_cannot_run_is_unavailable_not_absent(self) -> None:
        def stalled(command: Sequence[str], timeout: float) -> GitResult:
            raise git_client.GitError("git did not finish within 300 seconds", kind="timeout")

        services = rc.Services(git=GitClient(stalled))
        root = self.repository("topic")
        for name, call in (
            ("resolve", lambda: rc.resolve(services, root, "HEAD")),
            ("origin_has", lambda: rc.origin_has(services, root, "main")),
            ("current_branch", lambda: rc.current_branch(services, root)),
        ):
            with self.subTest(name), self.assertRaisesRegex(rc.GitUnavailable, "^could not run git: git did not"):
                call()

    def test_no_default_branch_skips_the_repository_on_its_branch(self) -> None:
        pattern = (
            "^cannot determine default branch: origin/HEAD is unset and origin has no main or master; left on topic$"
        )
        with self.assertRaisesRegex(rc.CleanupError, pattern):
            rc.default_branch(rc.Services(), self.repository("topic"))

    def renamed(self) -> Path:
        """A clone whose origin renamed its default branch from master to main after the clone, fetched and pruned,
        so its origin/HEAD still names master, which origin no longer has."""
        root = Path(tempfile.mkdtemp(prefix="repo cleanup renamed ")).resolve()
        self.addCleanup(remove_tree, root)
        upstream, remote, clone = root / "upstream work", root / "remote store.git", root / "renamed clone"
        git(root, "init", "--quiet", "-b", "master", str(upstream))
        git(upstream, "commit", "--quiet", "--allow-empty", "-m", "initial")
        git(root, "clone", "--quiet", "--bare", str(upstream), str(remote))
        git(root, "clone", "--quiet", str(remote), str(clone))
        bare_git(remote, "branch", "-m", "master", "main")
        git(clone, "fetch", "--quiet", "--prune", "origin")
        self.assertEqual("refs/remotes/origin/master", git(clone, "symbolic-ref", "refs/remotes/origin/HEAD"))
        self.assertEqual("", git(clone, "for-each-ref", "refs/remotes/origin/master"), "the fetch pruned it")
        return clone

    def test_a_dangling_origin_head_is_refreshed_from_a_reachable_origin(self) -> None:
        clone = self.renamed()
        note = "origin/HEAD named master, which origin lacks: refreshed it from origin"
        self.assertEqual(rc.DefaultBranch("main", note), rc.default_branch(rc.Services(), clone))
        self.assertEqual("refs/remotes/origin/main", git(clone, "symbolic-ref", "refs/remotes/origin/HEAD"))
        self.assertEqual(rc.DefaultBranch("main"), rc.default_branch(rc.Services(), clone), "nothing left to refresh")

    def test_a_dangling_origin_head_falls_back_to_main_when_origin_is_unreachable(self) -> None:
        clone = self.renamed()
        git(clone, "remote", "set-url", "origin", (clone.parent / "missing remote.git").as_posix())
        found = rc.default_branch(rc.Services(), clone)
        self.assertEqual("main", found.name)
        prefix = "origin/HEAD names master, which origin lacks: fell back to main — refreshing it failed: "
        self.assertTrue(found.note.startswith(prefix) and len(found.note) > len(prefix), found.note)
        self.assertEqual("refs/remotes/origin/master", git(clone, "symbolic-ref", "refs/remotes/origin/HEAD"))

    def test_a_refused_refresh_gives_gits_whole_first_line_as_its_reason(self) -> None:
        # git ends a line with "\n" alone; a U+2028 a ref name holds is part of the line, not the end of it.
        def refused(command: Sequence[str], timeout: float) -> GitResult:
            return GitResult(1, "", "error: not a ref: refs/remotes/origin/a\u2028b\nhint: second line\n")

        reason = rc.refresh_origin_head(rc.Services(git=GitClient(refused)), self.repository("topic"))
        self.assertEqual((None, rc.one_line("error: not a ref: refs/remotes/origin/a\u2028b")), reason)

    def test_a_dangling_origin_head_with_nothing_to_fall_back_on_leaves_the_repository_on_its_branch(self) -> None:
        root = self.repository("topic")
        git(root, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        pattern = (
            r"^cannot determine default branch: origin/HEAD names gone, which origin lacks, refreshing it failed "
            r"\(.+\), and origin has no main or master; left on topic$"
        )
        with self.assertRaisesRegex(rc.CleanupError, pattern):
            rc.default_branch(rc.Services(), root)
        self.assertEqual("refs/heads/topic", git(root, "symbolic-ref", "HEAD"))

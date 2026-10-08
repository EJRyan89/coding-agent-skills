from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import git_client
import repo_cleanup as rc
from git_client import GitClient, GitError, GitResult
from github_client import MISSING_CLI, CommandResult, GitHubError

REMOTE_URL = "https://github.com/owner/repo.git"
# The repositories every Fixture test starts from, built once per process by setUpModule.
TEMPLATE: Path | None = None
# What a sweep step returns.
T = TypeVar("T")


def setUpModule() -> None:
    """Isolate Git from the user's configuration for this process and every Git it starts."""
    directory = Path(tempfile.mkdtemp(prefix="repo-cleanup-config-"))
    config = directory / "gitconfig"
    config.write_text(
        "[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[core]\n\tautocrlf = false\n",
        encoding="utf-8",
    )
    patch = {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    saved = {name: os.environ.get(name) for name in patch}
    os.environ.update(patch)
    global TEMPLATE
    TEMPLATE = Path(tempfile.mkdtemp(prefix="repo-cleanup-template-")).resolve() / "root"
    build_fixture(TEMPLATE)

    def restore() -> None:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        remove_tree(directory)
        remove_tree(TEMPLATE.parent)

    unittest.addModuleCleanup(restore)


def remove_tree(path: Path) -> None:
    def writable(function, target, *_):
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def fixture_paths(root: Path) -> tuple[Path, Path, Path]:
    """The bare remote, the other clone that plays a collaborator, and the clone under REPOS_ROOT."""
    return root / "remote store" / "repo.git", root / "other clone", root / "Repos Root" / "my repo"


def build_fixture(root: Path) -> None:
    """A bare remote with one commit on main, a collaborator's clone of it, and a clone under a REPOS_ROOT with
    spaces whose github.com origin is rewritten to the bare remote."""
    remote, other, clone = fixture_paths(root)
    (root / "Repos Root").mkdir(parents=True)
    # An empty template leaves out the sample hooks, most of each repository's files and nothing Git runs, so each
    # test's copy of the three is smaller.
    git(root, "init", "--quiet", "--bare", "--template=", "-b", "main", str(remote))
    git(root, "clone", "--quiet", "--template=", str(remote), str(other))
    git(other, "commit", "--quiet", "--allow-empty", "-m", "initial")
    git(other, "push", "--quiet", "origin", "main")
    git(root, "clone", "--quiet", "--template=", str(remote), str(clone))
    git(clone, "remote", "set-url", "origin", REMOTE_URL)
    git(clone, "config", f"url.{remote.as_posix()}.insteadOf", REMOTE_URL)


def copy_fixture(root: Path) -> None:
    """A copy of the template under `root`, whose clones reach only the copy's own bare remote.

    Building the repositories starts eight git processes and copying them starts two, so each test gets a copy.
    The two clones name the bare remote by absolute path, so both are pointed at the copy's remote.
    """
    if TEMPLATE is None:
        raise AssertionError("setUpModule builds the template")
    shutil.copytree(TEMPLATE, root, symlinks=True, dirs_exist_ok=True)
    template_remote = fixture_paths(TEMPLATE)[0]
    remote, other, clone = fixture_paths(root)
    git(other, "remote", "set-url", "origin", str(remote))
    git(clone, "config", "--rename-section", f"url.{template_remote.as_posix()}", f"url.{remote.as_posix()}")


def facts(lines: list[str], kind: str) -> list[list[str]]:
    return [line.split("\t")[1:] for line in lines if line.split("\t")[0] == kind]


def plan_lines(plan: dict, path: Path) -> list[str]:
    """A plan's decisions as fact lines, so tests read what it decided the way they read every other step."""
    lines = [
        ["BRANCH", branch["name"], branch["category"], branch["pr"], branch["sha"], branch["action"], branch["detail"]]
        for branch in plan["branches"]
    ]
    lines += [
        ["WORKTREE", entry["path"], entry["branch"], entry["action"], entry["detail"]] for entry in plan["worktrees"]
    ]
    lines += [["FASTFORWARD", entry["branch"], entry["target"]] for entry in plan["fastforward"]]
    lines += [
        ["FF_SKIPPED", entry["branch"], entry["behind"], entry["reason"]] for entry in plan["fastforward_skipped"]
    ]
    lines.append(["PLAN", str(path)])
    return ["\t".join(str(field) if str(field).strip() else "-" for field in line) for line in lines]


class FakeGitHub:
    """Stands in for gh: answers the github.com access probe, `pr list` from a table of pull requests per branch, and
    the pull request commits query as `gh api --paginate --slurp` prints it, from `git rev-list --parents` lines."""

    def __init__(self) -> None:
        self.pulls: dict[str, list[dict]] = {}
        self.commits: dict[int, str] = {}
        self.failing: set[str] = set()
        self.authenticated = True
        self.access_failure: CommandResult | GitHubError | None = None  # what the access probe meets instead
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> CommandResult:
        if command[0] != "gh":
            raise AssertionError(command)
        arguments = list(command[1:])
        self.calls.append(arguments)
        if arguments == ["api", "--hostname", "github.com", "rate_limit"]:
            if isinstance(self.access_failure, GitHubError):
                raise self.access_failure
            if self.access_failure is not None:
                return self.access_failure
            if not self.authenticated:
                return CommandResult(4, "", "To get started with GitHub CLI, please run:  gh auth login\n")
            return CommandResult(0, "{}", "")
        if arguments[0] == "api":
            if arguments[:3] != ["api", "--paginate", "--slurp"]:
                raise AssertionError(arguments)
            number = int(arguments[3].split("/")[4])
            commits = [
                {"sha": sha, "parents": [{"sha": parent} for parent in parents]}
                for sha, *parents in (line.split() for line in self.commits.get(number, "").splitlines())
            ]
            return CommandResult(0, json.dumps([commits]), "")
        branch = arguments[arguments.index("--head") + 1]
        if branch in self.failing:
            return CommandResult(1, "", "HTTP 502")
        return CommandResult(0, json.dumps(self.pulls.get(branch, [])), "")

    def pull(self, branch: str, state: str, sha: str, commits: str = "") -> None:
        number = sum(len(pulls) for pulls in self.pulls.values()) + 1
        self.pulls.setdefault(branch, []).append(
            {
                "number": number,
                "state": state,
                "headRefOid": sha,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "owner"},
            }
        )
        self.commits[number] = commits


class Fixture(unittest.TestCase):
    """A clone under a REPOS_ROOT with spaces, whose github.com origin is rewritten to a local bare remote."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="repo cleanup ")).resolve()
        self.addCleanup(remove_tree, self.root)
        self.repos = self.root / "Repos Root"
        self.area = self.repos / "Worktrees" / "my repo"
        self.remote = self.root / "remote store" / "repo.git"
        self.other = self.root / "other clone"
        self.clone = self.repos / "my repo"
        copy_fixture(self.root)
        self.github = FakeGitHub()
        self.plan_file = self.root / "plans" / "my repo.json"

    # Git scenarios

    def commit(self, directory: Path, message: str) -> str:
        git(directory, "commit", "--quiet", "--allow-empty", "-m", message)
        return git(directory, "rev-parse", "HEAD")

    def tip(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.clone), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None

    def push_branch(self, name: str) -> str:
        git(self.clone, "switch", "--quiet", "-c", name, "main")
        tip = self.commit(self.clone, f"work on {name}")
        git(self.clone, "push", "--quiet", "-u", "origin", name)
        git(self.clone, "switch", "--quiet", "main")
        return tip

    def merge_remotely(self, name: str) -> None:
        git(self.other, "fetch", "--quiet", "origin")
        git(self.other, "merge", "--quiet", "--no-ff", "-m", f"Merge {name}", f"origin/{name}")
        git(self.other, "push", "--quiet", "origin", "main")

    def delete_remotely(self, name: str) -> None:
        git(self.other, "push", "--quiet", "origin", "--delete", name)

    def advance_remotely(self, name: str) -> str:
        git(self.other, "fetch", "--quiet", "origin")
        new = git(self.other, "commit-tree", f"origin/{name}^{{tree}}", "-p", f"origin/{name}", "-m", "more")
        git(self.other, "push", "--quiet", "origin", f"{new}:refs/heads/{name}")
        return new

    def commit_locally(self, name: str) -> str:
        new = git(self.clone, "commit-tree", f"{name}^{{tree}}", "-p", name, "-m", "local work")
        git(self.clone, "update-ref", f"refs/heads/{name}", new)
        return new

    def finished_branch(self, name: str, *, merged: bool = True, state: str | None = "MERGED") -> str:
        """A branch whose pull request finished at its tip and whose remote branch was deleted."""
        tip = self.push_branch(name)
        if merged:
            self.merge_remotely(name)
        self.delete_remotely(name)
        if state:
            self.github.pull(name, state, tip)
        return tip

    def updated_and_squashed(self, name: str, *, extra_work: bool = False) -> str:
        """A branch whose pull request gained a merge of main ("Update branch"), and optionally a later commit
        with real changes, before it was squash-merged and its remote branch deleted."""
        tip = self.push_branch(name)
        git(self.other, "fetch", "--quiet", "origin")
        git(self.other, "switch", "--quiet", "main")
        git(self.other, "merge", "--quiet", "--ff-only", "origin/main")
        self.commit(self.other, f"main moves on before {name} merges")
        git(self.other, "push", "--quiet", "origin", "main")
        git(self.other, "switch", "--quiet", "-c", name, f"origin/{name}")
        git(self.other, "merge", "--quiet", "--no-ff", "-m", f"Merge branch 'main' into {name}", "main")
        if extra_work:
            (self.other / f"{name}.txt").write_text("more work\n", encoding="utf-8")
            git(self.other, "add", f"{name}.txt")
            self.commit(self.other, f"more work on {name}")
        head = git(self.other, "rev-parse", "HEAD")
        commits = git(self.other, "rev-list", "--reverse", "--parents", f"main..{name}")
        git(self.other, "switch", "--quiet", "main")
        git(self.other, "branch", "--quiet", "-D", name)
        self.commit(self.other, f"Squash-merge {name}")
        git(self.other, "push", "--quiet", "origin", "main")
        self.delete_remotely(name)
        self.github.pull(name, "MERGED", head, commits + "\n")
        return tip

    # Commands

    def invoke(self, *arguments: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rc.main(list(arguments), rc.Services(gh=self.github))
        return code, output.getvalue().splitlines()

    def step(self, action: Callable[[rc.Services], T]) -> tuple[T, list[str]]:
        """Run one of the steps a sweep takes for each repository, capturing the lines it prints."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = action(rc.Services(gh=self.github))
        return result, output.getvalue().splitlines()

    def sync(self, skip_checkout: bool = False) -> tuple[bool, list[str]]:
        """Whether sync finished, so the sweep goes on to plan and apply, and the lines it printed."""
        return self.step(lambda services: rc.sync(str(self.clone), skip_checkout, services))

    def plan(self) -> tuple[int, list[str]]:
        plan = rc.build_plan(str(self.clone), str(self.repos), rc.Services(gh=self.github))
        rc.save_plan(self.plan_file, plan)
        return 0, plan_lines(plan, self.plan_file)

    def apply(self) -> tuple[int, list[str]]:
        _, lines = self.step(lambda services: rc.apply(str(self.plan_file), services))
        return 0, lines

    def confirm(self, command: str, *branches: str) -> tuple[int, list[str]]:
        arguments = [command, "--plan", str(self.plan_file)]
        for branch in branches:
            arguments += ["--branch", branch]
        return self.invoke(*arguments)

    def clean(self) -> tuple[list[str], list[str]]:
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        _, planned = self.plan()
        _, applied = self.apply()
        return planned, applied

    def branch_fact(self, planned: list[str], name: str) -> list[str]:
        return next(fields for fields in facts(planned, "BRANCH") if fields[0] == name)


class FixtureTemplateTests(unittest.TestCase):
    def test_a_copied_fixture_matches_a_freshly_built_one_and_reaches_only_its_own_remote(self) -> None:
        def shape(root: Path) -> dict[str, str]:
            remote, other, clone = fixture_paths(root)

            def local(text: str) -> str:
                return text.replace(str(root), "<root>").replace(root.as_posix(), "<root>")

            return {
                f"{name} {aspect}": local(git(repository, *arguments))
                for name, repository in (("remote", remote), ("other", other), ("clone", clone))
                for aspect, arguments in (
                    ("refs", ("for-each-ref", "--format=%(refname) %(tree) %(subject) %(upstream)")),
                    ("config", ("config", "--local", "--list")),
                )
            } | {"clone status": git(clone, "status", "--porcelain", "--branch")}

        fresh = Path(tempfile.mkdtemp(prefix="repo cleanup fresh ")).resolve()
        copied = Path(tempfile.mkdtemp(prefix="repo cleanup copy ")).resolve()
        self.addCleanup(remove_tree, fresh)
        self.addCleanup(remove_tree, copied)
        build_fixture(fresh)
        copy_fixture(copied)
        self.assertEqual(shape(fresh), shape(copied))

        if TEMPLATE is None:
            self.fail("setUpModule builds the template")
        remote, other, clone = fixture_paths(copied)
        git(other, "commit", "--quiet", "--allow-empty", "-m", "only in this copy")
        git(other, "push", "--quiet", "origin", "main")
        pushed = git(remote, "rev-parse", "main")
        self.assertNotEqual(pushed, git(fixture_paths(TEMPLATE)[0], "rev-parse", "main"))
        self.assertEqual(pushed, git(clone, "ls-remote", "origin", "refs/heads/main").split()[0])
        configs = [path / ".git" / "config" for path in (other, clone)] + [remote / "config"]
        for config in configs:
            text = config.read_text(encoding="utf-8")
            self.assertNotIn(TEMPLATE.as_posix(), text)
            self.assertNotIn(str(TEMPLATE).replace("\\", "\\\\"), text)


class DiscoverTests(Fixture):
    def discover(self, *target: str) -> list[str]:
        return rc.discover(target[0] if target else None, str(self.repos), rc.Services(gh=self.github))

    def test_sweep_lists_directories_that_contain_a_git_directory(self) -> None:
        (self.repos / "plain folder").mkdir()
        git(self.clone, "worktree", "add", "--quiet", "-b", "linked", str(self.repos / "linked worktree"))
        self.assertEqual([self.clone.as_posix()], self.discover())

    def test_target_by_name_or_absolute_path(self) -> None:
        for target in ("my repo", str(self.clone)):
            with self.subTest(target=target):
                self.assertEqual([self.clone.as_posix()], self.discover(target))

    def test_rejects_targets_that_are_not_repositories_or_names(self) -> None:
        (self.repos / "plain folder").mkdir()
        for target, message in (
            ("plain folder", "plain folder is not a git repository"),
            ("missing", "missing is not a git repository"),
            ("../my repo", "is not a repository name or an absolute path"),
            ("..", "is not a repository name or an absolute path"),
        ):
            with self.subTest(target=target), self.assertRaisesRegex(rc.CleanupError, re.escape(message)):
                self.discover(target)

    def test_unauthenticated_github_cli_stops(self) -> None:
        self.github.authenticated = False
        with self.assertRaisesRegex(rc.CleanupError, "^GitHub CLI not authenticated — run 'gh auth login'$"):
            self.discover()
        self.assertEqual([["api", "--hostname", "github.com", "rate_limit"]], self.github.calls)

    def test_the_reason_a_sweep_cannot_start_names_the_kind_of_failure(self) -> None:
        not_signed_in = "^GitHub CLI not authenticated — run 'gh auth login'$"
        for failure, message in (
            (CommandResult(1, "", "gh: Bad credentials (HTTP 401)\n"), not_signed_in),
            (GitHubError(MISSING_CLI, kind="prerequisite"), f"^GitHub CLI not installed — {re.escape(MISSING_CLI)}$"),
            (
                CommandResult(1, "", "error connecting to api.github.com\ncheck your internet connection\n"),
                "^cannot reach github.com — error connecting to api.github.com check your internet connection$",
            ),
            (
                GitHubError("GitHub CLI did not finish within 300 seconds", kind="timeout"),
                "^github.com did not answer — GitHub CLI did not finish within 300 seconds$",
            ),
            (
                CommandResult(1, "", "HTTP 502: Bad Gateway\n"),
                r"^GitHub CLI could not reach github.com \(api\) — HTTP 502",
            ),
        ):
            with self.subTest(failure=failure), self.assertRaisesRegex(rc.CleanupError, message):
                self.github.access_failure = failure
                self.discover()


class SyncTests(Fixture):
    def test_switches_to_default_fetches_prunes_and_fast_forwards(self) -> None:
        self.push_branch("pruned")
        git(self.clone, "switch", "--quiet", "-c", "topic")
        self.delete_remotely("pruned")
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        self.assertEqual([["main"]], facts(lines, "DEFAULT"))
        self.assertEqual([["switched"]], facts(lines, "CHECKOUT"))
        self.assertEqual([["ok"]], facts(lines, "FF_DEFAULT"))
        self.assertEqual("refs/heads/main", git(self.clone, "symbolic-ref", "HEAD"))
        self.assertEqual(git(self.other, "rev-parse", "HEAD"), self.tip("main"))
        self.assertEqual("", git(self.clone, "for-each-ref", "refs/remotes/origin/pruned"))

    def test_dirty_main_stops_before_switching_or_fetching_until_the_user_continues(self) -> None:
        git(self.clone, "switch", "--quiet", "-c", "topic")
        topic = self.tip("topic")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        before = git(self.clone, "rev-parse", "refs/remotes/origin/main")
        upstream = self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        finished, lines = self.sync()
        self.assertFalse(finished, lines)
        self.assertEqual([["1"]], facts(lines, "DIRTY_MAIN"))
        self.assertEqual([], facts(lines, "CHECKOUT") + facts(lines, "FF_DEFAULT"))
        self.assertEqual(before, git(self.clone, "rev-parse", "refs/remotes/origin/main"))
        finished, lines = self.sync(skip_checkout=True)
        self.assertTrue(finished, lines)
        self.assertEqual([["skipped"]], facts(lines, "CHECKOUT"))
        self.assertEqual([["ok"]], facts(lines, "FF_DEFAULT"))
        self.assertEqual("refs/heads/topic", git(self.clone, "symbolic-ref", "HEAD"))
        self.assertEqual(topic, self.tip("topic"), "the checked-out branch must not take the default's commits")
        self.assertEqual(upstream, self.tip("main"))
        self.assertTrue((self.clone / "draft.txt").is_file())

    def test_a_default_branch_checked_out_with_changes_is_not_fast_forwarded(self) -> None:
        local = self.tip("main")
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        finished, lines = self.sync(skip_checkout=True)
        self.assertTrue(finished, lines)
        self.assertEqual(
            [["failed", f"worktree {self.clone.as_posix()} has 1 uncommitted change"]], facts(lines, "FF_DEFAULT")
        )
        self.assertEqual(local, self.tip("main"))
        self.assertEqual(local, git(self.clone, "rev-parse", "HEAD"))

    def test_fetch_failure_stops_the_repository(self) -> None:
        git(self.clone, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(self.clone, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")
        finished, lines = self.sync()
        self.assertFalse(finished, lines)
        self.assertEqual(1, len(facts(lines, "FETCH_FAILED")))
        self.assertEqual([], facts(lines, "FF_DEFAULT"))
        summary = [fields[0] for fields in facts(lines, "SUMMARY")]
        self.assertTrue(summary[0].startswith("my repo skipped: fetch failed — "), summary)
        self.assertIn("nothing was fast-forwarded", summary[1])

    def test_diverged_default_is_reported_and_left_alone(self) -> None:
        local = self.commit(self.clone, "local main work")
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        self.assertEqual([["diverged", "1", "1"]], facts(lines, "FF_DEFAULT"))
        self.assertEqual(local, self.tip("main"))


class PlanApplyTests(Fixture):
    def test_gone_branches_are_deleted_only_when_their_pull_request_proves_them_stale(self) -> None:
        self.finished_branch("merged-gone")
        self.finished_branch("squashed-gone", merged=False)
        abandoned = self.finished_branch("abandoned-gone", merged=False, state="CLOSED")
        unproven = self.finished_branch("unproven-gone", merged=False, state=None)
        self.finished_branch("orphan-gone", state=None)
        self.finished_branch("open-gone", state="OPEN")
        self.finished_branch("failing-gone", state=None)
        self.github.failing.add("failing-gone")
        self.finished_branch("reused-gone", state=None)
        self.github.pull("reused-gone", "MERGED", "f" * 40)
        self.finished_branch("release/1.0")
        planned, applied = self.clean()
        actions = {fields[0]: (fields[1], fields[2], fields[4]) for fields in facts(planned, "BRANCH")}
        self.assertEqual(
            {
                "merged-gone": ("gone", "MERGED", "delete"),
                "squashed-gone": ("gone", "MERGED", "delete"),
                "abandoned-gone": ("gone", "CLOSED", "delete"),
                "unproven-gone": ("gone", "NONE", "delete"),
                "orphan-gone": ("gone", "NONE", "delete"),
                "open-gone": ("gone", "OPEN", "keep"),
                "failing-gone": ("gone", "UNKNOWN", "keep"),
                "reused-gone": ("gone", "UNMATCHED", "keep"),
                "release/1.0": ("gone", "-", "keep"),
            },
            actions,
        )
        self.assertEqual(
            {"merged-gone", "orphan-gone", "squashed-gone"}, {fields[0] for fields in facts(applied, "DELETED")}
        )
        self.assertEqual([["abandoned-gone", abandoned], ["unproven-gone", unproven]], facts(applied, "UNMERGED"))
        self.assertIsNone(self.tip("squashed-gone"), "a pull request merged at the exact tip proves a squash merge")
        for name in ("abandoned-gone", "unproven-gone", "open-gone", "failing-gone", "reused-gone", "release/1.0"):
            self.assertIsNotNone(self.tip(name), name)
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("my repo cleanup complete:", summary)
        self.assertIn("  Default branch:          main (up to date)", summary)
        self.assertIn("  Branches deleted:        3 — merged-gone, orphan-gone, squashed-gone", summary)
        self.assertIn("  PR status unverified:    1 — failing-gone (", summary)
        self.assertIn("  PR history unmatched:    1 — reused-gone (kept)", summary)
        self.assertIn("  Unmerged (kept):", summary)
        self.assertIn("  Gone with open PR:", summary)
        self.assertTrue(
            all(call[:2] == ["pr", "list"] or call[:2] == ["api", "--paginate"] for call in self.github.calls)
        )

        code, lines = self.confirm("force-delete", "abandoned-gone", "unproven-gone", "open-gone")
        self.assertEqual(0, code, lines)
        self.assertEqual([["abandoned-gone"], ["unproven-gone"]], facts(lines, "DELETED"))
        self.assertEqual([["open-gone", "not reported UNMERGED by this plan"]], facts(lines, "PRESERVED"))
        self.assertIsNone(self.tip("abandoned-gone"))
        self.assertIsNotNone(self.tip("open-gone"))
        summary = "\n".join(fields[0] for fields in facts(lines, "SUMMARY"))
        self.assertIn(
            "Branches deleted:        5 — merged-gone, orphan-gone, squashed-gone, abandoned-gone, unproven-gone",
            summary,
        )
        self.assertNotIn("Unmerged (kept)", summary)

    def test_branch_that_moved_after_planning_is_preserved(self) -> None:
        self.finished_branch("merged-gone")
        self.finished_branch("squashed-gone", merged=False)
        abandoned = self.finished_branch("abandoned-gone", merged=False, state="CLOSED")
        self.sync()
        self.plan()
        moved = self.commit_locally("merged-gone")
        squashed_moved = self.commit_locally("squashed-gone")
        code, lines = self.apply()
        self.assertEqual(0, code, lines)
        self.assertIn(["merged-gone", "moved"], facts(lines, "PRESERVED"))
        self.assertIn(["squashed-gone", "moved"], facts(lines, "PRESERVED"))
        self.assertEqual(moved, self.tip("merged-gone"))
        self.assertEqual(squashed_moved, self.tip("squashed-gone"), "a moved squash-merged branch is never forced")
        self.commit_locally("abandoned-gone")
        code, lines = self.confirm("force-delete", "abandoned-gone")
        self.assertEqual([["abandoned-gone", "moved"]], facts(lines, "PRESERVED"))
        self.assertNotEqual(abandoned, self.tip("abandoned-gone"))

    def test_pull_request_updated_from_main_before_merging_still_proves_the_branch_finished(self) -> None:
        self.updated_and_squashed("updated-gone")
        self.updated_and_squashed("updated-in-worktree")
        extended = self.updated_and_squashed("extended-gone", extra_work=True)
        updated_tree = self.area / "updated wt"
        extended_tree = self.area / "extended wt"
        git(self.clone, "worktree", "add", "--quiet", str(updated_tree), "updated-in-worktree")
        git(self.clone, "worktree", "add", "--quiet", str(extended_tree), "extended-gone")
        planned, applied = self.clean()
        actions = {fields[0]: (fields[1], fields[2], fields[4]) for fields in facts(planned, "BRANCH")}
        self.assertEqual(
            {
                "updated-gone": ("gone", "MERGED", "delete"),
                "updated-in-worktree": ("gone", "MERGED", "remove-worktree"),
                "extended-gone": ("gone", "UNMATCHED", "keep"),
            },
            actions,
        )
        self.assertEqual({"updated-gone", "updated-in-worktree"}, {fields[0] for fields in facts(applied, "DELETED")})
        self.assertEqual(
            {"updated wt": "updated-in-worktree"},
            {Path(fields[0]).name: fields[1] for fields in facts(applied, "REMOVED")},
        )
        self.assertIsNone(self.tip("updated-gone"))
        self.assertFalse(updated_tree.exists())
        self.assertEqual(extended, self.tip("extended-gone"), "a later commit with real changes keeps the branch")
        self.assertTrue(extended_tree.exists())
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("  PR history unmatched:    1 — extended-gone (kept)", summary)

    def test_local_only_branches_wait_for_confirmation(self) -> None:
        git(self.clone, "branch", "local-merged", "main")
        git(self.clone, "switch", "--quiet", "-c", "local-work", "main")
        work = self.commit(self.clone, "unpublished work")
        git(self.clone, "switch", "--quiet", "main")
        self.github.pull("local-work", "CLOSED", work)
        git(self.clone, "branch", "local-open", "main")
        self.github.pull("local-open", "OPEN", work)
        git(self.clone, "branch", "local-squashed", "local-work")
        squashed = self.commit_locally("local-squashed")
        self.github.pull("local-squashed", "MERGED", squashed)
        planned, applied = self.clean()
        self.assertEqual("ask-delete", self.branch_fact(planned, "local-merged")[4])
        self.assertEqual(["MERGED", squashed, "ask-delete"], self.branch_fact(planned, "local-squashed")[2:5])
        self.assertEqual("keep", self.branch_fact(planned, "local-open")[4])
        self.assertEqual([["local-merged"], ["local-squashed"], ["local-work"]], facts(applied, "CONFIRM_LOCAL"))
        self.assertEqual([], facts(applied, "DELETED"))

        code, lines = self.confirm("force-delete", "local-work")
        self.assertEqual([["local-work", "not reported UNMERGED by this plan"]], facts(lines, "PRESERVED"))
        code, lines = self.confirm("delete-local", "local-merged", "local-squashed", "local-work", "local-open")
        self.assertEqual(0, code, lines)
        self.assertEqual([["local-merged"], ["local-squashed"]], facts(lines, "DELETED"))
        self.assertEqual([["local-work", work]], facts(lines, "UNMERGED"))
        self.assertEqual([["local-open", "not a local-only branch offered by this plan"]], facts(lines, "PRESERVED"))
        code, lines = self.confirm("force-delete", "local-work")
        self.assertEqual([["local-work"]], facts(lines, "DELETED"))
        self.assertIsNone(self.tip("local-work"))
        self.assertIsNotNone(self.tip("local-open"))

    def test_apply_runs_once_and_confirmations_need_an_applied_plan(self) -> None:
        self.sync()
        self.plan()
        code, lines = self.confirm("delete-local", "anything")
        self.assertEqual((1, ["FAILED\trun apply with this plan first"]), (code, lines))
        self.apply()
        with self.assertRaisesRegex(rc.CleanupError, "^this plan was already applied; run plan again$"):
            self.apply()

    def test_a_confirmation_with_a_missing_plan_fails_with_one_line(self) -> None:
        code, lines = self.confirm("force-delete", "anything")
        self.assertEqual(1, code)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith(f"FAILED\tcannot read plan {self.plan_file}: "), lines)

    def test_plan_changes_nothing_in_the_repository(self) -> None:
        self.finished_branch("merged-gone")
        self.push_branch("behind")
        self.advance_remotely("behind")
        git(self.clone, "worktree", "add", "--quiet", str(self.area / "wt"), "merged-gone")
        self.assertTrue(self.sync()[0])

        def snapshot() -> tuple[str, ...]:
            return (
                git(self.clone, "for-each-ref", "--format=%(refname) %(objectname)"),
                git(self.clone, "worktree", "list", "--porcelain"),
                git(self.clone, "status", "--porcelain"),
                git(self.area / "wt", "status", "--porcelain"),
            )

        before = snapshot()
        code, lines = self.plan()
        self.assertEqual(0, code, lines)
        self.assertEqual(before, snapshot())
        self.assertEqual(
            [["behind", git(self.clone, "rev-parse", "refs/remotes/origin/behind")]], facts(lines, "FASTFORWARD")
        )
        self.assertEqual([[str(self.plan_file)]], facts(lines, "PLAN"))

    def test_worktrees_are_removed_only_when_clean_unprotected_and_stale(self) -> None:
        self.finished_branch("feature/clean")
        clean = self.area / "feature dir" / "clean wt"
        self.finished_branch("feature/squashed", merged=False)
        squashed = self.area / "squashed wt"
        self.finished_branch("feature/dirty")
        dirty = self.area / "dirty wt"
        self.finished_branch("hotfix")
        protected = self.area / "release" / "hotfix wt"
        tracked = self.push_branch("tracked-merged")
        self.merge_remotely("tracked-merged")
        self.github.pull("tracked-merged", "MERGED", tracked)
        outside = self.root / "outside" / "tracked wt"
        self.push_branch("tracked-work")
        kept = self.area / "kept wt"
        for path, branch in (
            (clean, "feature/clean"),
            (squashed, "feature/squashed"),
            (dirty, "feature/dirty"),
            (protected, "hotfix"),
            (outside, "tracked-merged"),
            (kept, "tracked-work"),
        ):
            git(self.clone, "worktree", "add", "--quiet", str(path), branch)
        (dirty / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        planned, applied = self.clean()
        worktrees = {Path(fields[0]).name: (fields[1], fields[2]) for fields in facts(planned, "WORKTREE")}
        self.assertEqual(
            {
                "clean wt": ("feature/clean", "remove"),
                "squashed wt": ("feature/squashed", "remove"),
                "dirty wt": ("feature/dirty", "dirty:1"),
                "hotfix wt": ("hotfix", "protected"),
                "tracked wt": ("tracked-merged", "remove"),
                "kept wt": ("tracked-work", "keep"),
            },
            worktrees,
        )
        self.assertEqual(["keep", "worktree-dirty"], self.branch_fact(planned, "feature/dirty")[4:6])
        self.assertEqual(["keep", "in-worktree"], self.branch_fact(planned, "tracked-work")[4:6])
        removed = {Path(fields[0]).name: fields[1] for fields in facts(applied, "REMOVED")}
        self.assertEqual(
            {"clean wt": "feature/clean", "squashed wt": "feature/squashed", "tracked wt": "tracked-merged"}, removed
        )
        self.assertEqual(
            {"feature/clean", "feature/squashed", "tracked-merged"}, {fields[0] for fields in facts(applied, "DELETED")}
        )
        self.assertEqual([], facts(applied, "UNMERGED"))
        self.assertFalse(clean.exists() or squashed.exists() or outside.exists())
        pruned = [fields[0] for fields in facts(applied, "PRUNED_DIR")]
        self.assertEqual(1, len(pruned), pruned)
        self.assertTrue(rc.same_path(pruned[0], str(self.area / "feature dir")))
        self.assertTrue((self.root / "outside").is_dir(), "directories outside the worktree area are kept")
        for path in (dirty / "notes.txt", protected, kept):
            self.assertTrue(path.exists(), path)
        for name in ("feature/dirty", "hotfix", "tracked-work"):
            self.assertIsNotNone(self.tip(name), name)
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("  Dirty worktrees skipped: 1 — ", summary)
        self.assertIn("  Protected release worktrees: 1 (preserved)", summary)

    def test_fast_forward_targets_each_branch_by_its_exact_name(self) -> None:
        self.push_branch("fix/a.b+c")
        self.push_branch("fix/aabc")
        self.push_branch("diverged")
        worktree = self.area / "aabc wt"
        git(self.clone, "worktree", "add", "--quiet", str(worktree), "fix/aabc")
        dotted = self.advance_remotely("fix/a.b+c")
        plain = self.advance_remotely("fix/aabc")
        self.advance_remotely("diverged")
        local = self.commit_locally("diverged")
        planned, applied = self.clean()
        self.assertEqual(sorted([["fix/a.b+c", dotted], ["fix/aabc", plain]]), sorted(facts(planned, "FASTFORWARD")))
        self.assertEqual(sorted([["fix/a.b+c", dotted], ["fix/aabc", plain]]), sorted(facts(applied, "FF")))
        self.assertEqual([["diverged", "1", "1"]], facts(applied, "DIVERGED"))
        self.assertEqual(dotted, self.tip("fix/a.b+c"))
        self.assertEqual(plain, self.tip("fix/aabc"))
        self.assertEqual(plain, git(worktree, "rev-parse", "HEAD"))
        self.assertEqual("", git(worktree, "status", "--porcelain"))
        self.assertEqual(local, self.tip("diverged"))

    def test_a_branch_checked_out_in_a_worktree_with_changes_is_never_fast_forwarded(self) -> None:
        linked = self.push_branch("feature")
        worktree = self.area / "feature wt"
        git(self.clone, "worktree", "add", "--quiet", str(worktree), "feature")
        self.advance_remotely("feature")
        (worktree / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        current = self.push_branch("topic")
        git(self.clone, "switch", "--quiet", "topic")
        self.advance_remotely("topic")
        self.advance_remotely("topic")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        finished, lines = self.sync(skip_checkout=True)
        self.assertTrue(finished, lines)
        _, planned = self.plan()
        _, applied = self.apply()
        self.assertEqual([], facts(planned, "FASTFORWARD"))
        self.assertEqual(
            [
                ["feature", "1", f"worktree {worktree.as_posix()} has 1 uncommitted change"],
                ["topic", "2", f"worktree {self.clone.as_posix()} has 1 uncommitted change"],
            ],
            facts(planned, "FF_SKIPPED"),
        )
        self.assertEqual([], facts(applied, "FF") + facts(applied, "PRESERVED"))
        self.assertEqual((linked, linked), (self.tip("feature"), git(worktree, "rev-parse", "HEAD")))
        self.assertEqual((current, current), (self.tip("topic"), git(self.clone, "rev-parse", "HEAD")))
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("  Branches fast-forwarded: 0\n", summary)
        self.assertIn(f"  Dirty worktrees skipped: 1 — {worktree.as_posix()} (feature, 1 changed)\n", summary)
        self.assertIn(
            f"  Fast-forward skipped:    2 — feature (behind 1; worktree {worktree.as_posix()} has 1 uncommitted "
            f"change), topic (behind 2; worktree {self.clone.as_posix()} has 1 uncommitted change)",
            summary,
        )

    def test_a_worktree_that_gains_changes_after_planning_is_not_fast_forwarded(self) -> None:
        linked = self.push_branch("feature")
        worktree = self.area / "feature wt"
        git(self.clone, "worktree", "add", "--quiet", str(worktree), "feature")
        target = self.advance_remotely("feature")
        self.sync()
        _, planned = self.plan()
        self.assertEqual([["feature", target]], facts(planned, "FASTFORWARD"))
        (worktree / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        _, applied = self.apply()
        self.assertEqual(
            [["feature", f"fast-forward failed: worktree {worktree.as_posix()} has 1 uncommitted change"]],
            facts(applied, "PRESERVED"),
        )
        self.assertEqual(linked, git(worktree, "rev-parse", "HEAD"))

    def test_a_gone_branch_is_judged_against_the_default_branch_when_head_is_elsewhere(self) -> None:
        # git branch -d judges a branch whose upstream is gone against HEAD, which --skip-checkout leaves on a topic.
        abandoned = self.finished_branch("abandoned", merged=False, state="CLOSED")
        self.finished_branch("merged-elsewhere", state=None)
        git(self.clone, "switch", "--quiet", "-c", "stacked", "abandoned")
        self.commit(self.clone, "work stacked on the abandoned branch")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        finished, lines = self.sync(skip_checkout=True)
        self.assertTrue(finished, lines)
        _, planned = self.plan()
        _, applied = self.apply()
        self.assertEqual("delete", self.branch_fact(planned, "abandoned")[4])
        self.assertEqual([["merged-elsewhere"]], facts(applied, "DELETED"))
        self.assertEqual([["abandoned", abandoned]], facts(applied, "UNMERGED"))
        self.assertEqual(abandoned, self.tip("abandoned"), "HEAD holds it, but the default branch does not")
        self.assertIsNone(self.tip("merged-elsewhere"), "the default branch holds it, although HEAD does not")

    def test_origin_that_is_not_on_github_leaves_branches_unverified(self) -> None:
        self.finished_branch("merged-gone")
        git(self.clone, "remote", "set-url", "origin", self.remote.as_posix())
        planned, applied = self.clean()
        fields = self.branch_fact(planned, "merged-gone")
        self.assertEqual(
            ["UNKNOWN", fields[3], "keep", "pr-unknown: origin is not a github.com repository"], fields[2:]
        )
        self.assertEqual([], facts(applied, "DELETED"))
        self.assertEqual([], self.github.calls)


class SweepTests(Fixture):
    def make_clone(self, name: str) -> Path:
        clone = self.repos / name
        git(self.root, "clone", "--quiet", str(self.remote), str(clone))
        git(clone, "remote", "set-url", "origin", REMOTE_URL)
        git(clone, "config", f"url.{self.remote.as_posix()}.insteadOf", REMOTE_URL)
        return clone

    def sweep(self, services: rc.Services | None = None, *extra: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rc.main(
                ["sweep", "--repos-root", str(self.repos), "--plans", str(self.root / "plans"), *extra],
                services or rc.Services(gh=self.github),
            )
        return code, output.getvalue().splitlines()

    def test_a_dirty_repository_the_user_continues_is_cleaned_in_one_sweep_without_switching(self) -> None:
        # One command instead of sync, plan, and apply: three commands would cost the agent two more calls, and a
        # shell variable naming the script would make Claude Code ask before each.
        done = self.finished_branch("done")
        git(self.clone, "switch", "--quiet", "-c", "topic")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        self.make_clone("other repo")

        code, lines = self.sweep(None, "--skip-checkout", "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(lines, "REPO"))
        self.assertEqual([["1"]], facts(lines, "DIRTY_MAIN"))
        self.assertEqual([[(self.root / "plans" / "my repo.json").as_posix()]], facts(lines, "PLAN"))
        self.assertIsNone(self.tip("done"), f"{done} should be deleted")
        self.assertEqual("refs/heads/topic", git(self.clone, "symbolic-ref", "HEAD"))
        self.assertTrue((self.clone / "draft.txt").is_file())
        self.assertFalse((self.root / "plans" / "other repo.json").exists(), "only the target is swept")

    def test_sweep_keeps_its_plans_in_a_new_temporary_directory_by_default(self) -> None:
        self.finished_branch("done")
        temporary = self.root / "tmp"
        temporary.mkdir()
        output = io.StringIO()
        with mock.patch.object(tempfile, "tempdir", str(temporary)), contextlib.redirect_stdout(output):
            code = rc.main(["sweep", "--repos-root", str(self.repos), "my repo"], rc.Services(gh=self.github))
        lines = output.getvalue().splitlines()
        self.assertEqual(0, code, lines)
        self.assertEqual("PLANS", lines[0].split("\t")[0], "the directory comes first")
        plans = Path(facts(lines, "PLANS")[0][0])
        self.assertEqual(temporary, plans.parent)
        self.assertTrue(plans.name.startswith("repo-cleanup-plans-"), plans)
        self.assertEqual([[(plans / "my repo.json").as_posix()]], facts(lines, "PLAN"))
        self.assertTrue((plans / "my repo.json").is_file())

    def test_sweep_refuses_a_plans_directory_inside_a_skill_tree(self) -> None:
        skills_root = Path(rc.__file__).resolve().parents[2]
        home = self.root / "home"
        targets = [
            skills_root / "repo-cleanup" / "plans-from-a-test",  # beside SKILL.md
            skills_root / "review-prs" / "plans-from-a-test",
            home / ".claude" / "skills" / "repo-cleanup" / "plans",
            home / ".agents" / "skills" / "repo-cleanup" / "plans",
        ]
        with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
            for target in targets:
                self.addCleanup(shutil.rmtree, target, ignore_errors=True)  # if a regression wrote it
                with self.subTest(target=target):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = rc.main(
                            ["sweep", "--repos-root", str(self.repos), "--plans", str(target)],
                            rc.Services(gh=self.github),
                        )
                    lines = output.getvalue().splitlines()
                    self.assertEqual(1, code)
                    self.assertEqual(1, len(lines), lines)
                    self.assertTrue(lines[0].startswith(f"FAILED\t{target} is inside the skills directory "), lines)
                    self.assertFalse(target.exists())
        self.assertEqual([], self.github.calls)

    def test_an_explicit_plans_directory_outside_every_skill_tree_is_used_and_printed(self) -> None:
        code, lines = self.sweep(None, "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[(self.root / "plans").as_posix()]], facts(lines, "PLANS"))
        self.assertEqual([[(self.root / "plans" / "my repo.json").as_posix()]], facts(lines, "PLAN"))

    def blocks(self, lines: list[str]) -> dict[str, list[str]]:
        """Each repository's lines, keyed by its folder name, without the REPO header."""
        blocks: dict[str, list[str]] = {}
        current: list[str] = []
        for line in lines:
            if line.startswith("REPO\t"):
                current = blocks.setdefault(Path(line.split("\t")[1]).name, [])
            elif not line.startswith("SWEPT\t"):
                current.append(line)
        return blocks

    def test_one_sweep_cleans_every_repository_and_reports_only_what_needs_the_agent(self) -> None:
        done = self.finished_branch("done")
        git(self.clone, "branch", "scratch", "main")
        self.make_clone("quiet repo")
        dirty = self.make_clone("dirty repo")
        (dirty / "draft.txt").write_text("draft\n", encoding="utf-8")
        broken = self.make_clone("broken repo")
        git(broken, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(broken, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")

        code, lines = self.sweep()
        self.assertEqual(1, code, "a dirty or unfetched repository needs the agent")
        root = self.repos.as_posix()
        self.assertEqual(
            [
                [f"{root}/broken repo", "fetch-failed"],
                [f"{root}/dirty repo", "dirty"],
                [f"{root}/my repo", "cleaned"],
                [f"{root}/quiet repo", "quiet"],
            ],
            facts(lines, "REPO"),
        )
        self.assertEqual(
            [["4", "cleaned=1", "quiet=1", "dirty=1", "fetch-failed=1", "error=0", "git-failed=0"]],
            facts(lines, "SWEPT"),
        )
        self.assertEqual(
            {"PLANS", "REPO", "PLAN", "SWEPT", "SUMMARY", "DIRTY_MAIN", "FETCH_FAILED", "CONFIRM_LOCAL"},
            {line.split("\t")[0] for line in lines},
            "plan items and step chatter stay out",
        )

        blocks = self.blocks(lines)
        plan = (self.root / "plans" / "my repo.json").as_posix()
        self.assertEqual([[plan]], facts(blocks["my repo"], "PLAN"))
        self.assertEqual([["scratch"]], facts(blocks["my repo"], "CONFIRM_LOCAL"))
        summary = [fields[0] for fields in facts(blocks["my repo"], "SUMMARY")]
        self.assertEqual("my repo cleanup complete:", summary[0])
        self.assertIn("Branches deleted:        1 — done", "\n".join(summary))
        self.assertIsNone(self.tip("done"), f"{done} should be deleted")
        self.assertEqual([[(self.root / "plans" / "quiet repo.json").as_posix()]], facts(blocks["quiet repo"], "PLAN"))
        self.assertEqual([], facts(blocks["quiet repo"], "SUMMARY"), "a quiet repository's summary is folded")
        self.assertEqual([["1"]], facts(blocks["dirty repo"], "DIRTY_MAIN"))
        self.assertEqual([], facts(blocks["dirty repo"], "PLAN"))
        self.assertEqual(1, len(facts(blocks["broken repo"], "FETCH_FAILED")))
        self.assertTrue(facts(blocks["broken repo"], "SUMMARY")[0][0].startswith("broken repo skipped: fetch failed"))

        self.plan_file = self.root / "plans" / "my repo.json"
        code, confirmed = self.confirm("delete-local", "scratch")
        self.assertEqual(0, code, confirmed)
        self.assertEqual([["scratch"]], facts(confirmed, "DELETED"))

    def test_repositories_are_fetched_at_the_same_time(self) -> None:
        for name in ("second repo", "third repo"):
            self.make_clone(name)
        active = peak = 0
        changed = threading.Condition()

        def git_runner(command: Sequence[str], timeout: float) -> GitResult:
            nonlocal active, peak
            if "fetch" not in command:
                return git_client.subprocess_runner(command, timeout)
            with changed:
                active += 1
                peak = max(peak, active)
                changed.notify_all()
                changed.wait_for(lambda: active >= 2, timeout=10)  # the last fetch may find no partner
            try:
                return git_client.subprocess_runner(command, timeout)
            finally:
                with changed:
                    active -= 1

        code, lines = self.sweep(rc.Services(git=GitClient(git_runner), gh=self.github))
        self.assertEqual(0, code, lines)
        self.assertGreaterEqual(peak, 2)

    def test_a_git_failure_reports_every_repository_then_exits_1(self) -> None:
        self.make_clone("other repo")

        def git_runner(command: Sequence[str], timeout: float) -> GitResult:
            if command[2].endswith("other repo"):
                raise GitError("git vanished", kind="execution")
            return git_client.subprocess_runner(command, timeout)

        code, lines = self.sweep(rc.Services(git=GitClient(git_runner), gh=self.github))
        self.assertEqual(1, code)
        root = self.repos.as_posix()
        self.assertEqual([[f"{root}/my repo", "quiet"], [f"{root}/other repo", "git-failed"]], facts(lines, "REPO"))
        self.assertEqual([["could not run git: git vanished"]], facts(self.blocks(lines)["other repo"], "ERROR"))

    def test_a_fetch_that_never_finishes_fails_its_repository_and_the_sweep_goes_on(self) -> None:
        # A private remote with an expired credential used to wait on a prompt; now the fetch is bounded.
        self.make_clone("stale repo")
        timeouts: list[float] = []

        def git_runner(command: Sequence[str], timeout: float) -> GitResult:
            if "fetch" in command:
                timeouts.append(timeout)
                if command[2].endswith("stale repo"):
                    raise GitError("git fetch did not finish within 300 seconds", kind="timeout")
            return git_client.subprocess_runner(command, timeout)

        code, lines = self.sweep(rc.Services(git=GitClient(git_runner), gh=self.github))
        self.assertEqual(1, code)
        root = self.repos.as_posix()
        self.assertEqual([[f"{root}/my repo", "quiet"], [f"{root}/stale repo", "git-failed"]], facts(lines, "REPO"))
        self.assertEqual(
            [["could not run git: git fetch did not finish within 300 seconds"]],
            facts(self.blocks(lines)["stale repo"], "ERROR"),
        )
        self.assertEqual([300.0, 300.0], timeouts)

    def test_a_branch_named_with_a_unicode_line_separator_is_planned_and_every_repository_reported(self) -> None:
        self.make_clone("other repo")
        name = "odd\u2028name"
        git(self.clone, "branch", name, "main")
        code, lines = self.sweep()
        self.assertEqual(0, code, lines)
        root = self.repos.as_posix()
        self.assertEqual([[f"{root}/my repo", "cleaned"], [f"{root}/other repo", "quiet"]], facts(lines, "REPO"))
        self.assertEqual([["odd name"]], facts(self.blocks(lines)["my repo"], "CONFIRM_LOCAL"))
        self.assertTrue(all("\u2028" not in line for line in lines), "every fact stays on one line")
        plan = json.loads((self.root / "plans" / "my repo.json").read_text(encoding="utf-8"))
        self.assertEqual([name], [branch["name"] for branch in plan["branches"]])

    def test_an_unexpected_failure_reports_that_repository_and_every_other_then_exits_1(self) -> None:
        self.finished_branch("done")
        self.make_clone("other repo")
        build_plan = rc.build_plan

        def failing(root: str, repos_root: str, services: rc.Services) -> dict[str, Any]:
            if root.endswith("other repo"):
                raise ValueError("not enough values to unpack (expected 4, got 1)")
            return build_plan(root, repos_root, services)

        with mock.patch.object(rc, "build_plan", failing):
            code, lines = self.sweep()
        self.assertEqual(1, code, lines)
        root = self.repos.as_posix()
        self.assertEqual([[f"{root}/my repo", "cleaned"], [f"{root}/other repo", "error"]], facts(lines, "REPO"))
        blocks = self.blocks(lines)
        self.assertEqual(
            [["unexpected ValueError: not enough values to unpack (expected 4, got 1)"]],
            facts(blocks["other repo"], "ERROR"),
        )
        self.assertIn(
            "Branches deleted:        1 — done", "\n".join(fields[0] for fields in facts(blocks["my repo"], "SUMMARY"))
        )
        self.assertEqual(
            [["2", "cleaned=1", "quiet=0", "dirty=0", "fetch-failed=0", "error=1", "git-failed=0"]],
            facts(lines, "SWEPT"),
        )

    def test_a_sweep_that_cannot_start_fails_with_one_line(self) -> None:
        self.github.authenticated = False
        code, lines = self.sweep()
        self.assertEqual(1, code, lines)
        self.assertEqual(["PLANS", "FAILED"], [line.split("\t")[0] for line in lines])
        self.assertEqual("FAILED\tGitHub CLI not authenticated — run 'gh auth login'", lines[-1])

    def test_only_the_commands_the_skill_runs_remain(self) -> None:
        for command in ("discover", "sync", "plan", "apply"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    rc.main([command], rc.Services(gh=self.github))
                self.assertEqual(2, raised.exception.code)


class DefaultBranchTests(unittest.TestCase):
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
        self.assertEqual("trunk", rc.default_branch(rc.Services(), root))

    def test_without_origin_head_main_then_master(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                root = self.repository("topic")
                git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
                self.assertEqual(branch, rc.default_branch(rc.Services(), root))

    def test_no_default_branch_skips_the_repository(self) -> None:
        with self.assertRaisesRegex(rc.CleanupError, "^cannot determine default branch$"):
            rc.default_branch(rc.Services(), self.repository("topic"))


class RemoveWorktreeTests(unittest.TestCase):
    """git worktree remove, then git branch -d, as apply runs them for a stale worktree."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="repo cleanup remove ")).resolve()
        self.addCleanup(remove_tree, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "-b", "main")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "initial")
        self.plan: dict[str, Any] = {"repo_root": str(self.repo), "default": {"name": "main"}, "events": []}

    def tip(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None

    def add(self, name: str) -> Path:
        path = self.root / f"{name} wt"
        git(self.repo, "worktree", "add", "--quiet", "-b", name, str(path))
        return path

    def remove(self, name: str, pr: str = "CLOSED") -> tuple[bool, list[list[str]], Path]:
        path = self.root / f"{name} wt"
        entry = {"path": str(path), "branch": name}
        tip = self.tip(name)
        if tip is None:
            self.fail(f"{name} was added with a branch")
        with contextlib.redirect_stdout(io.StringIO()):
            removed = rc.remove_worktree(rc.Services(), self.plan, entry, tip, pr)
        return removed, self.plan["events"], path

    def test_a_merged_worktree_and_its_branch_are_removed(self) -> None:
        self.add("merged")
        removed, events, path = self.remove("merged")
        self.assertTrue(removed)
        self.assertEqual([["REMOVED", str(path), "merged"], ["DELETED", "merged"]], events)
        self.assertFalse(path.exists())
        self.assertIsNone(self.tip("merged"))

    def test_a_locked_worktree_is_kept_with_gits_reason(self) -> None:
        path = self.add("locked")
        (path / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        git(path, "add", "sentinel.txt")
        git(path, "commit", "--quiet", "-m", "sentinel")
        git(self.repo, "worktree", "lock", str(path))
        removed, events, _ = self.remove("locked")
        self.assertFalse(removed)
        self.assertEqual(["PRESERVED", "locked"], events[0][:2])
        self.assertTrue(events[0][2].startswith(f"Git refused to remove worktree {path}: "), events)
        self.assertTrue((path / "sentinel.txt").is_file())
        self.assertIsNotNone(self.tip("locked"))

    def test_untracked_work_is_kept(self) -> None:
        path = self.add("untracked")
        (path / "draft.txt").write_text("draft\n", encoding="utf-8")
        removed, events, _ = self.remove("untracked")
        self.assertFalse(removed)
        self.assertEqual(["PRESERVED", "untracked"], events[0][:2])
        self.assertTrue((path / "draft.txt").is_file())

    def test_an_unmerged_branch_outlives_its_removed_worktree(self) -> None:
        path = self.add("unmerged")
        git(path, "commit", "--quiet", "--allow-empty", "-m", "work")
        sha = self.tip("unmerged")
        removed, events, _ = self.remove("unmerged")
        self.assertTrue(removed)
        self.assertEqual([["REMOVED", str(path), "unmerged"], ["UNMERGED", "unmerged", sha]], events)
        self.assertFalse(path.exists())
        self.assertEqual(sha, self.tip("unmerged"))


class UnitTests(unittest.TestCase):
    def test_release_branches_and_worktrees_under_a_release_directory_are_protected(self) -> None:
        for branch, path, expected in (
            ("release/4.10", "C:/GitHub/Worktrees/example/topic", True),
            ("topic/fix", "C:/GitHub/Worktrees/example/release/4.10", True),
            ("topic/fix", "C:\\GitHub\\Worktrees\\example\\release\\4.10", True),
            ("topic/fix", "C:/release/example", True),
            ("topic/fix", "/release/example", True),
            ("topic/release-notes", "C:/GitHub/Worktrees/example/topic", False),
            ("topic/fix", "C:/GitHub/Worktrees/example/pre-release/topic", False),
            ("releases/1", "C:/GitHub/Worktrees/example/released", False),
        ):
            with self.subTest(branch=branch, path=path):
                self.assertEqual(expected, rc.is_protected(branch, path))

    def test_github_remote_urls(self) -> None:
        for url, expected in (
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://user@github.com/Owner/my.repo.git", "Owner/my.repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("https://gitlab.com/owner/repo.git", None),
            ("https://github.com/owner/repo/extra", None),
            ("C:/remotes/repo.git", None),
        ):
            with self.subTest(url=url):
                match = rc.GITHUB_REMOTE.fullmatch(url)
                self.assertEqual(expected, match.group(1) if match else None)

    def test_empty_directory_pruning_stays_inside_its_boundary(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup prune ")).resolve()
        self.addCleanup(remove_tree, root)
        area = root / "Worktrees"
        (area / "my repo" / "nested").mkdir(parents=True)
        self.assertEqual(
            [(area / "my repo" / "nested").as_posix(), (area / "my repo").as_posix()],
            rc.prune_empty_parents(str(area / "my repo" / "nested" / "removed"), [str(area)]),
        )
        self.assertTrue(area.is_dir(), "the worktree area itself is never removed")

        (area / "busy" / "empty").mkdir(parents=True)
        (area / "busy" / "keep.txt").write_text("x", encoding="utf-8")
        self.assertEqual(
            [(area / "busy" / "empty").as_posix()],
            rc.prune_empty_parents(str(area / "busy" / "empty" / "removed"), [str(area)]),
        )
        self.assertTrue((area / "busy" / "keep.txt").is_file())

        (root / "elsewhere" / "empty").mkdir(parents=True)
        self.assertEqual([], rc.prune_empty_parents(str(root / "elsewhere" / "empty" / "removed"), [str(area)]))
        self.assertTrue((root / "elsewhere" / "empty").is_dir())
        self.assertEqual([], rc.prune_empty_parents(str(area), [str(area)]))

    def test_dirty_count_counts_a_rename_once(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup status ")).resolve()
        self.addCleanup(remove_tree, root)
        git(root, "init", "--quiet")
        (root / "old name.txt").write_text("content\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "--quiet", "-m", "initial")
        git(root, "mv", "old name.txt", "new name.txt")
        (root / "untracked.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(2, rc.dirty_count(rc.Services(), root))


if __name__ == "__main__":
    unittest.main()

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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import cleanup_policy
import git_client
import repo_cleanup as rc
from git_client import GitClient, GitError, GitResult
from github_client import MISSING_CLI, CommandResult, GitHubError

REMOTE_URL = "https://github.com/owner/repo.git"
# The suite's configuration refuses a bare repository Git is not pointed at explicitly, as an agent sandbox's may, so
# every test holds that a bare fixture is inspected with `bare_git`.
GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main", "safe.bareRepository": "explicit"})
# The repositories every Fixture test starts from, built once per process by setUpModule.
TEMPLATE: Path | None = None
# What a sweep step returns.
T = TypeVar("T")
# The reason an offer whose name does not survive one line is preserved instead, given the flattened name.
UNNAMEABLE = "not offered: the name {} holds a character a fact line cannot carry, so no confirmation could name it"


# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG
# The plans directory a sweep makes when it names no record, which Fixture replaces with one in the test's folder.
NEW_PLANS_DIRECTORY = rc.new_plans_directory


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


def build_template() -> None:
    """Build the repositories every Fixture test starts from, once per process: the setUpModule of each suite whose
    tests use Fixture calls it after isolating Git."""
    global TEMPLATE
    TEMPLATE = Path(tempfile.mkdtemp(prefix="repo-cleanup-template-")).resolve() / "root"
    build_fixture(TEMPLATE)
    unittest.addModuleCleanup(remove_tree, TEMPLATE.parent)


class CleanupTestCase(unittest.TestCase):
    git_config = GIT_CONFIG

    def setUp(self) -> None:
        # The seals a plan's save writes go to a throwaway folder of the test's, never the user's temporary directory.
        seals = Path(tempfile.mkdtemp(prefix="repo cleanup seals ")).resolve()
        self.addCleanup(remove_tree, seals)
        patcher = mock.patch.object(rc, "SEALS", seals)
        patcher.start()
        self.addCleanup(patcher.stop)


def remove_tree(path: Path) -> None:
    def writable(function: Callable[..., object], target: str, *_: object) -> None:
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def git(directory: Path, *arguments: str) -> str:
    return run_git("-C", str(directory), *arguments)


def bare_git(repository: Path, *arguments: str) -> str:
    """Git in a bare repository, named with `--git-dir` because `safe.bareRepository=explicit` refuses `-C` into one."""
    return run_git(f"--git-dir={repository}", *arguments)


def run_git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], capture_output=True, text=True, encoding="utf-8")
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


def written_plan(**fields: Any) -> dict[str, Any]:
    """A plan holding every field build_plan writes, for a repository at a folder that need not exist, with `fields`
    in place of its own."""
    return {
        "schema_version": 1,
        "repo_root": "/repos/my repo",
        "repo_name": "my repo",
        "worktree_area": "/repos/Worktrees",
        "orphan_areas": ["/repos/Worktrees/my repo", "/repos/my repo/.claude/worktrees"],
        "default": {"name": "main", "status": "up to date"},
        "branches": [],
        "worktrees": [],
        "fastforward": [],
        "fastforward_skipped": [],
        "diverged": [],
        "policy": {"regenerable": "", "protected": ""},
        "applied": True,
        "events": [],
        **fields,
    }


def is_regenerable(entry: str, tree: Path, policy: cleanup_policy.Policy) -> bool:
    """Whether a worktree removal deletes the ignored `entry` of the worktree at `tree` with it under `policy`."""
    regenerable, _ = rc.regenerable_under(rc.Services(), str(tree), [entry], policy)
    return entry in regenerable


def facts(lines: list[str], kind: str) -> list[list[str]]:
    return [line.split("\t")[1:] for line in lines if line.split("\t")[0] == kind]


def plan_lines(plan: dict[str, Any], path: Path) -> list[str]:
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


def refusing_branch_deletion(*names: str) -> GitClient:
    """Git that refuses every `git branch` command ending in one of these branches, as a locked ref makes it, and
    runs every other command."""

    def refusing(command: Sequence[str], timeout: float) -> GitResult:
        if "branch" in command and command[-1] in names:
            return GitResult(1, "", f"error: cannot lock ref 'refs/heads/{command[-1]}'")
        return git_client.subprocess_runner(command, timeout)

    return GitClient(refusing)


class FakeGitHub:
    """Stands in for gh: answers the github.com access probe, `pr list` from a table of pull requests per branch, and
    the pull request commits query as `gh api --paginate --slurp` prints it, from `git rev-list --parents` lines."""

    def __init__(self) -> None:
        self.pulls: dict[str, list[dict[str, Any]]] = {}
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
            return CommandResult(1, "", "HTTP 500")
        return CommandResult(0, json.dumps(self.pulls.get(branch, [])), "")

    def pull(
        self,
        branch: str,
        state: str,
        sha: str,
        commits: str = "",
        merge_commit: str | None = None,
        base: str = "main",
    ) -> None:
        number = sum(len(pulls) for pulls in self.pulls.values()) + 1
        self.pulls.setdefault(branch, []).append(
            {
                "number": number,
                "state": state,
                "headRefOid": sha,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "owner"},
                "mergeCommit": None if merge_commit is None else {"oid": merge_commit},
                "baseRefName": base,
            }
        )
        self.commits[number] = commits


class Fixture(CleanupTestCase):
    """A clone under a REPOS_ROOT with spaces, whose github.com origin is rewritten to a local bare remote."""

    def setUp(self) -> None:
        super().setUp()
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
        # A sweep that names no record keeps its plans in the test's own folder, never the user's temporary directory.
        patcher = mock.patch.object(rc, "new_plans_directory", self.plans_directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plans_directory(self) -> Path:
        plans = self.root / "plans"
        plans.mkdir(exist_ok=True)
        return plans

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
        """A branch off main with one empty commit, pushed and tracking its remote branch; main stays checked out.
        The commit is written without checking the branch out, which would cost two more git processes."""
        tip = git(self.clone, "commit-tree", "main^{tree}", "-p", "main", "-m", f"work on {name}")
        git(self.clone, "branch", "--quiet", name, tip)
        git(self.clone, "push", "--quiet", "-u", "origin", name)
        return tip

    def merge_remotely(self, name: str, *, delete: bool = False) -> None:
        """Merge the remote branch into main from the other clone, and delete the remote branch in the same push when
        `delete` is set."""
        git(self.other, "fetch", "--quiet", "origin")
        git(self.other, "merge", "--quiet", "--no-ff", "-m", f"Merge {name}", f"origin/{name}")
        git(self.other, "push", "--quiet", "origin", "main", *([f":refs/heads/{name}"] if delete else []))

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
            self.merge_remotely(name, delete=True)
        else:
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
        git(self.other, "push", "--quiet", "origin", "main", f":refs/heads/{name}")
        self.github.pull(name, "MERGED", head, commits + "\n")
        return tip

    def rewritten_and_squashed(self, name: str, *, extra_work: bool = False) -> str:
        """A branch with a file change whose pull request was squash-merged and its remote branch deleted, then
        whose local tip was reworded without changing its content, and optionally extended with a real change."""
        git(self.clone, "switch", "--quiet", "-c", name, "main")
        (self.clone / f"{name}.txt").write_text(f"work on {name}\n", encoding="utf-8")
        git(self.clone, "add", f"{name}.txt")
        tip = self.commit(self.clone, f"work on {name}")
        git(self.clone, "push", "--quiet", "-u", "origin", name)
        git(self.other, "fetch", "--quiet", "origin")
        git(self.other, "switch", "--quiet", "main")
        git(self.other, "merge", "--quiet", "--ff-only", "origin/main")
        git(self.other, "merge", "--quiet", "--squash", f"origin/{name}")
        squash = self.commit(self.other, f"Squash-merge {name}")
        git(self.other, "push", "--quiet", "origin", "main", f":refs/heads/{name}")
        rewritten = git(self.clone, "commit-tree", f"{name}^{{tree}}", "-p", f"{name}^", "-m", f"reworded {name}")
        git(self.clone, "update-ref", f"refs/heads/{name}", rewritten)
        if extra_work:
            (self.clone / f"{name}.txt").write_text(f"more work on {name}\n", encoding="utf-8")
            git(self.clone, "add", f"{name}.txt")
            rewritten = self.commit(self.clone, f"more work on {name}")
        git(self.clone, "switch", "--quiet", "main")
        self.github.pull(name, "MERGED", tip, merge_commit=squash)
        return rewritten

    def remote_release(self, base: str, *, orphan: bool = False) -> None:
        """A release branch on the remote, from main with a release-only commit, or sharing no history with it."""
        if orphan:
            git(self.other, "checkout", "--quiet", "--orphan", base)
            git(self.other, "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", ".")
            for path in self.other.iterdir():
                if path.is_file():
                    path.unlink()
        else:
            git(self.other, "switch", "--quiet", "-c", base, "main")
        (self.other / "notes.txt").write_text(f"notes for {base}\n", encoding="utf-8")
        git(self.other, "add", "notes.txt")
        self.commit(self.other, f"notes for {base}")
        git(self.other, "push", "--quiet", "-u", "origin", base)
        git(self.other, "switch", "--quiet", "main")
        git(self.clone, "fetch", "--quiet", "origin")

    def squashed_into(self, name: str, base: str) -> str:
        """A branch off the fetched `base` with a file change, squash-merged into `base` by the collaborator and
        deleted remotely, then reworded locally without changing its content; the reworded tip."""
        git(self.clone, "switch", "--quiet", "-c", name, f"origin/{base}")
        (self.clone / f"{name}.txt").write_text(f"work on {name}\n", encoding="utf-8")
        git(self.clone, "add", f"{name}.txt")
        tip = self.commit(self.clone, f"work on {name}")
        git(self.clone, "push", "--quiet", "-u", "origin", name)
        git(self.other, "fetch", "--quiet", "origin")
        git(self.other, "switch", "--quiet", base)
        git(self.other, "merge", "--quiet", "--squash", f"origin/{name}")
        squash = self.commit(self.other, f"Squash-merge {name}")
        git(self.other, "push", "--quiet", "origin", base, f":refs/heads/{name}")
        git(self.other, "switch", "--quiet", "main")
        rewritten = git(self.clone, "commit-tree", f"{name}^{{tree}}", "-p", f"{name}^", "-m", f"reworded {name}")
        git(self.clone, "update-ref", f"refs/heads/{name}", rewritten)
        git(self.clone, "switch", "--quiet", "main")
        self.github.pull(name, "MERGED", tip, merge_commit=squash, base=base)
        return rewritten

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

    def sync(self, skip_checkout: bool = False) -> tuple[dict[str, Any] | None, list[str]]:
        """What sync did, or None when it stopped before the sweep plans and applies, and the lines it printed."""
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


class FixtureTemplateTests(CleanupTestCase):
    def test_git_reads_only_the_suites_configuration_which_refuses_a_bare_repository_not_named_explicitly(self) -> None:
        if TEMPLATE is None:
            self.fail("setUpModule builds the template")
        listing = git(TEMPLATE.parent, "config", "--list", "--show-scope").splitlines()
        self.assertEqual({"global"}, {line.split("\t", 1)[0] for line in listing}, listing)
        remote, other, _ = fixture_paths(TEMPLATE)
        with self.assertRaisesRegex(AssertionError, "safe.bareRepository"):
            git(remote, "rev-parse", "main")
        self.assertEqual(git(other, "rev-parse", "origin/main"), bare_git(remote, "rev-parse", "main"))

    def test_a_copied_fixture_matches_a_freshly_built_one_and_reaches_only_its_own_remote(self) -> None:
        def shape(root: Path) -> dict[str, str]:
            remote, other, clone = fixture_paths(root)

            def local(text: str) -> str:
                return text.replace(str(root), "<root>").replace(root.as_posix(), "<root>")

            return {
                f"{name} {aspect}": local(run(repository, *arguments))
                for name, repository, run in (
                    ("remote", remote, bare_git),
                    ("other", other, git),
                    ("clone", clone, git),
                )
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
        pushed = bare_git(remote, "rev-parse", "main")
        self.assertNotEqual(pushed, bare_git(fixture_paths(TEMPLATE)[0], "rev-parse", "main"))
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
        for failure, message, waits in (
            (CommandResult(1, "", "gh: Bad credentials (HTTP 401)\n"), not_signed_in, []),
            (
                GitHubError(MISSING_CLI, kind="prerequisite"),
                f"^GitHub CLI not installed — {re.escape(MISSING_CLI)}$",
                [],
            ),
            (
                CommandResult(1, "", "error connecting to api.github.com\ncheck your internet connection\n"),
                "^cannot reach github.com — error connecting to api.github.com check your internet connection$",
                [],
            ),
            (
                GitHubError("GitHub CLI did not finish within 300 seconds", kind="timeout"),
                "^github.com did not answer — GitHub CLI did not finish within 300 seconds$",
                [],
            ),
            (
                CommandResult(1, "", "HTTP 500: Internal Server Error\n"),
                r"^GitHub CLI access check failed \(api\) — HTTP 500",
                [],
            ),
            (
                CommandResult(1, "", "gh: API rate limit exceeded (HTTP 403)\n"),
                "^GitHub's rate limit refused the GitHub CLI — gh: API rate limit exceeded "
                r".*\(gave up after 5 retries\)$",
                [5.0, 10.0, 20.0, 40.0, 80.0],
            ),
        ):
            # discover builds its own GitHubClient, which takes time.sleep as its sleeper when it is built, so the
            # patch records the waits a rate limit's backoff would make instead of sleeping through them.
            with self.subTest(failure=failure), mock.patch("time.sleep") as sleep:
                self.github.access_failure = failure
                with self.assertRaisesRegex(rc.CleanupError, message):
                    self.discover()
                self.assertEqual(waits, [call.args[0] for call in sleep.call_args_list])


class SyncTests(Fixture):
    def test_switches_to_default_fetches_prunes_and_fast_forwards(self) -> None:
        self.push_branch("pruned")
        git(self.clone, "switch", "--quiet", "-c", "topic")
        self.delete_remotely("pruned")
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        self.assertEqual([["switched"]], facts(lines, "CHECKOUT"))
        forwarded = ["forwarded", git(self.other, "rev-parse", "HEAD")]
        self.assertEqual([forwarded], facts(lines, "FF_DEFAULT"))
        self.assertEqual(
            {"default": {"name": "main", "note": ""}, "checkout": "switched", "fast_forward": forwarded, "behind": 1},
            finished,
        )
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
        self.assertEqual([["forwarded", upstream]], facts(lines, "FF_DEFAULT"))
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

    def test_a_switch_git_refuses_is_reported_and_the_sync_goes_on(self) -> None:
        git(self.clone, "switch", "--quiet", "-c", "topic")
        git(self.clone, "worktree", "add", "--quiet", str(self.area / "main wt"), "main")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        checkout = facts(lines, "CHECKOUT")
        self.assertEqual(1, len(checkout), lines)
        self.assertEqual("failed", checkout[0][0])
        self.assertIn("main", checkout[0][1], "Git's reason names the branch it could not switch to")
        self.assertEqual([["current"]], facts(lines, "FF_DEFAULT"))
        self.assertEqual("refs/heads/topic", git(self.clone, "symbolic-ref", "HEAD"))
        self.assertEqual("failed", finished["checkout"] if finished else None)

    def test_fetch_failure_stops_the_repository_before_it_is_switched(self) -> None:
        git(self.clone, "switch", "--quiet", "-c", "topic")
        git(self.clone, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(self.clone, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")
        finished, lines = self.sync()
        self.assertFalse(finished, lines)
        self.assertEqual(1, len(facts(lines, "FETCH_FAILED")))
        self.assertEqual([], facts(lines, "CHECKOUT") + facts(lines, "FF_DEFAULT"))
        self.assertEqual("refs/heads/topic", git(self.clone, "symbolic-ref", "HEAD"))
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
        self.assertIn("  Open PR (kept):          1 — open-gone", summary)
        self.assertTrue(
            all(call[:2] == ["pr", "list"] or call[:2] == ["api", "--paginate"] for call in self.github.calls)
        )

        code, lines = self.confirm("force-delete", "abandoned-gone", "unproven-gone", "open-gone")
        self.assertEqual(1, code, "a branch the plan never reported UNMERGED needs the agent")
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

    def test_a_tip_rewritten_after_its_pull_request_squash_merged_is_still_deleted(self) -> None:
        self.rewritten_and_squashed("reworded-gone")
        extended = self.rewritten_and_squashed("extended-gone", extra_work=True)
        planned, applied = self.clean()
        actions = {fields[0]: (fields[1], fields[2], fields[4]) for fields in facts(planned, "BRANCH")}
        self.assertEqual(
            {"reworded-gone": ("gone", "MERGED", "delete"), "extended-gone": ("gone", "UNMATCHED", "keep")}, actions
        )
        self.assertEqual([["reworded-gone"]], facts(applied, "DELETED"))
        self.assertIsNone(self.tip("reworded-gone"))
        self.assertEqual(extended, self.tip("extended-gone"), "a later commit with real changes keeps the branch")
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("  PR history unmatched:    1 — extended-gone (kept)", summary)

    def test_a_pull_request_merged_into_another_base_still_deletes_its_branch(self) -> None:
        self.remote_release("release/1.0")
        self.remote_release("release/old", orphan=True)
        self.squashed_into("hotfix-gone", "release/1.0")
        self.squashed_into("oldfix-gone", "release/old")
        # A squash-merged branch that tracks main itself, checked out clean, with main's tip as its upstream.
        tracking = self.rewritten_and_squashed("tracks-main")
        git(self.clone, "branch", "--set-upstream-to=origin/main", "tracks-main")
        tracking_tree = self.area / "tracking wt"
        git(self.clone, "worktree", "add", "--quiet", str(tracking_tree), "tracks-main")
        extended = self.rewritten_and_squashed("extended-gone", extra_work=True)
        planned, applied = self.clean()
        actions = {fields[0]: (fields[1], fields[2], fields[4]) for fields in facts(planned, "BRANCH")}
        self.assertEqual(
            {
                "hotfix-gone": ("gone", "MERGED", "delete"),
                "oldfix-gone": ("gone", "MERGED", "delete"),
                "tracks-main": ("tracking", "MERGED", "remove-worktree"),
                "extended-gone": ("gone", "UNMATCHED", "keep"),
            },
            actions,
        )
        self.assertEqual(
            {"hotfix-gone", "oldfix-gone", "tracks-main"}, {fields[0] for fields in facts(applied, "DELETED")}
        )
        self.assertEqual(
            {"tracking wt": "tracks-main"}, {Path(fields[0]).name: fields[1] for fields in facts(applied, "REMOVED")}
        )
        for name in ("hotfix-gone", "oldfix-gone", "tracks-main"):
            self.assertIsNone(self.tip(name))
        self.assertFalse(tracking_tree.exists())
        self.assertNotEqual(tracking, extended)
        self.assertEqual(extended, self.tip("extended-gone"), "a later commit with real changes keeps the branch")
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn("  PR history unmatched:    1 — extended-gone (kept)", summary)
        self.assertNotIn("PR status unverified:    1", summary)
        self.assertEqual(
            {"release/1.0", "release/old"},
            set(git(self.clone, "branch", "-r", "--format=%(refname:lstrip=3)").split("\n")) - {"main", "HEAD"},
            "the release branches stay on the remote; no local branch of them was made",
        )

    def test_the_summary_names_caches_deleted_with_a_worktree_and_paths_that_keep_one(self) -> None:
        (self.clone / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.env\n", encoding="utf-8")
        git(self.clone, "add", ".gitignore")
        self.commit(self.clone, "ignore caches and secrets")
        git(self.clone, "push", "--quiet", "origin", "main")
        git(self.other, "pull", "--quiet", "--ff-only", "origin", "main")
        self.finished_branch("cached-gone")
        self.finished_branch("secret-gone")
        cached_tree, secret_tree = self.area / "cached wt", self.area / "secret wt"
        git(self.clone, "worktree", "add", "--quiet", str(cached_tree), "cached-gone")
        git(self.clone, "worktree", "add", "--quiet", str(secret_tree), "secret-gone")
        for tree in (cached_tree, secret_tree):
            (tree / "__pycache__").mkdir()
            (tree / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
            (tree / ".pytest_cache").mkdir()
            (tree / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
        (secret_tree / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        _, applied = self.clean()
        self.assertEqual(
            {"cached wt": "cached-gone"},
            {Path(fields[0]).name: fields[1] for fields in facts(applied, "REMOVED")},
        )
        self.assertEqual(
            [["deleted 2 regenerable paths: .pytest_cache/, __pycache__/"]],
            [fields[2:] for fields in facts(applied, "REMOVED")],
        )
        [(preserved_branch, preserved_reason)] = facts(applied, "PRESERVED")
        [(removed_path, *_)] = facts(applied, "REMOVED")
        self.assertEqual("secret-gone", preserved_branch)
        self.assertEqual("secret wt", Path(removed_path.replace("cached", "secret")).name)
        self.assertEqual(
            f"worktree {removed_path.replace('cached', 'secret')} holds 1 ignored path git worktree remove would "
            "delete: .env; also 2 regenerable paths: .pytest_cache/, __pycache__/",
            preserved_reason,
        )
        self.assertFalse(cached_tree.exists())
        self.assertTrue((secret_tree / ".env").is_file())
        self.assertIsNone(self.tip("cached-gone"))
        self.assertIsNotNone(self.tip("secret-gone"))
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn(
            f"  Worktrees removed:       1 — {removed_path} (cached-gone; deleted 2 regenerable paths: "
            ".pytest_cache/, __pycache__/)",
            summary,
        )
        self.assertIn(f"  Preserved:               1 — secret-gone: {preserved_reason}", summary)

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
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        self.assertIn(
            "  Open PR (kept):          1 — local-open", summary, "an open PR is reported with or without a remote"
        )

        code, lines = self.confirm("force-delete", "local-work")
        self.assertEqual(1, code, "a branch the plan never reported UNMERGED needs the agent")
        self.assertEqual([["local-work", "not reported UNMERGED by this plan"]], facts(lines, "PRESERVED"))
        code, lines = self.confirm("delete-local", "local-merged", "local-squashed", "local-work", "local-open")
        self.assertEqual(1, code, "a branch the plan never offered needs the agent")
        self.assertEqual([["local-merged"], ["local-squashed"]], facts(lines, "DELETED"))
        self.assertEqual([["local-work", work]], facts(lines, "UNMERGED"))
        self.assertEqual(
            [["local-open", "not a local-only or closed branch offered by this plan"]], facts(lines, "PRESERVED")
        )
        code, lines = self.confirm("force-delete", "local-work")
        self.assertEqual((0, [["local-work"]]), (code, facts(lines, "DELETED")), lines)
        self.assertIsNone(self.tip("local-work"))
        code, lines = self.confirm("delete-local", "local-merged")
        self.assertEqual(0, code, "a branch the plan already deleted needs nothing more")
        self.assertEqual([["local-merged", "already deleted by this plan"]], facts(lines, "PRESERVED"))
        self.assertIsNotNone(self.tip("local-open"))

    def test_apply_runs_once_and_confirmations_need_an_applied_plan(self) -> None:
        self.sync()
        self.plan()
        code, lines = self.confirm("delete-local", "anything")
        self.assertEqual(
            (1, ["FAILED\tthis plan was not applied by a sweep; run the sweep again for a new plan"]), (code, lines)
        )
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(self.area))
        self.assertEqual(
            (1, ["FAILED\tthis plan was not applied by a sweep; run the sweep again for a new plan"]), (code, lines)
        )
        self.apply()
        with self.assertRaisesRegex(
            rc.CleanupError, "^this plan was already applied; run the sweep again for a new plan$"
        ):
            self.apply()

    def test_a_refusal_of_a_plan_names_only_a_command_the_parser_has(self) -> None:
        for message in (f"this plan was not applied by a sweep; {rc.SWEEP_AGAIN}", rc.SWEEP_AGAIN):
            commands = re.findall(r"\brun (?:the )?([a-z-]+)", message)
            self.assertEqual(["sweep"], commands, message)
        for command, code in (("sweep", 0), ("apply", 2), ("plan", 2)):
            with (
                self.subTest(command=command),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                rc.build_parser().parse_args([command, "--help"])
            self.assertEqual(code, raised.exception.code, command)

    def test_a_deletion_git_refuses_exits_1_after_reporting_every_branch(self) -> None:
        git(self.clone, "branch", "first", "main")
        git(self.clone, "switch", "--quiet", "-c", "second", "main")
        second = self.commit(self.clone, "unpublished work")
        git(self.clone, "switch", "--quiet", "main")
        self.clean()
        services = rc.Services(git=refusing_branch_deletion("first", "second"), gh=self.github)
        for command, branch in (("delete-local", "first"), ("delete-local", "second"), ("force-delete", "second")):
            with self.subTest(command=command, branch=branch):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = rc.main([command, "--plan", str(self.plan_file), "--branch", branch], services)
                lines = output.getvalue().splitlines()
                if branch == "second" and command == "delete-local":
                    self.assertEqual((0, [["second", second]]), (code, facts(lines, "UNMERGED")))
                    continue
                self.assertEqual(1, code, lines)
                self.assertEqual(
                    [[branch, f"error: cannot lock ref 'refs/heads/{branch}'"]], facts(lines, "PRESERVED")[-1:]
                )
                self.assertTrue(facts(lines, "SUMMARY"), "the summary is printed with the failure")
        self.assertIsNotNone(self.tip("first"))
        self.assertIsNotNone(self.tip("second"))

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
        self.assertIn("  Protected worktrees:     1 (preserved)", summary)

    def test_a_worktree_whose_pull_request_closed_unmerged_is_removed_and_its_branch_asked_about(self) -> None:
        tracked = self.push_branch("closed-tracked")
        self.github.pull("closed-tracked", "CLOSED", tracked)
        gone = self.finished_branch("closed-gone", merged=False, state="CLOSED")
        trees = {"closed-tracked": self.area / "tracked wt", "closed-gone": self.area / "gone wt"}
        for branch, path in trees.items():
            git(self.clone, "worktree", "add", "--quiet", str(path), branch)
        planned, applied = self.clean()
        for branch in trees:
            self.assertEqual("remove-worktree-ask", self.branch_fact(planned, branch)[4], branch)
        self.assertEqual(
            {"tracked wt": "closed-tracked", "gone wt": "closed-gone"},
            {Path(fields[0]).name: fields[1] for fields in facts(applied, "REMOVED")},
        )
        self.assertFalse(any(path.exists() for path in trees.values()))
        self.assertEqual([], facts(applied, "DELETED"), "a closed pull request's branch is never deleted unasked")
        self.assertEqual([["closed-gone"], ["closed-tracked"]], sorted(facts(applied, "CONFIRM_LOCAL")))
        self.assertEqual((tracked, gone), (self.tip("closed-tracked"), self.tip("closed-gone")))
        summary = "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        row = next(line for line in summary.splitlines() if line.startswith("  Closed PR (kept):        2 — "))
        self.assertEqual({"closed-tracked", "closed-gone"}, set(row.split(" — ")[1].split(", ")))
        self.assertNotIn("Local-only (kept)", summary)

        code, lines = self.confirm("delete-local", "closed-tracked", "closed-gone")
        self.assertEqual(0, code, lines)
        self.assertEqual([["closed-tracked", tracked], ["closed-gone", gone]], facts(lines, "UNMERGED"))
        code, lines = self.confirm("force-delete", "closed-gone")
        self.assertEqual([["closed-gone"]], facts(lines, "DELETED"))
        self.assertIsNone(self.tip("closed-gone"))
        self.assertEqual(tracked, self.tip("closed-tracked"), "a branch the user keeps stays")

    def test_a_closed_branch_whose_worktree_git_refused_to_remove_is_not_offered(self) -> None:
        tip = self.push_branch("closed-locked")
        self.github.pull("closed-locked", "CLOSED", tip)
        path = self.area / "locked wt"
        git(self.clone, "worktree", "add", "--quiet", str(path), "closed-locked")
        git(self.clone, "worktree", "lock", str(path))
        _, applied = self.clean()
        self.assertEqual([], facts(applied, "CONFIRM_LOCAL"))
        _, lines = self.confirm("delete-local", "closed-locked")
        self.assertEqual(
            [["closed-locked", "not a local-only or closed branch offered by this plan"]], facts(lines, "PRESERVED")
        )
        self.assertEqual(tip, self.tip("closed-locked"))
        git(self.clone, "worktree", "unlock", str(path))

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
                ["sweep", "--repos-root", str(self.repos), *extra],
                services or rc.Services(gh=self.github),
            )
        return code, output.getvalue().splitlines()

    def test_a_clone_of_a_checkout_on_a_topic_branch_is_switched_to_origins_default_not_the_stale_branch(
        self,
    ) -> None:
        git(self.other, "switch", "--quiet", "-c", "topic/old")
        stale = self.commit(self.other, "work only the other checkout has")
        copied = self.repos / "copied repo"
        git(self.root, "clone", "--quiet", str(self.other), str(copied))
        git(self.other, "switch", "--quiet", "main")
        git(copied, "remote", "set-url", "origin", REMOTE_URL)
        git(copied, "config", f"url.{self.remote.as_posix()}.insteadOf", REMOTE_URL)
        git(copied, "fetch", "--quiet", "--prune", "origin")
        self.assertEqual("refs/remotes/origin/topic/old", git(copied, "symbolic-ref", "refs/remotes/origin/HEAD"))

        code, lines = self.sweep(None, "copied repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[copied.as_posix(), "cleaned"]], facts(lines, "REPO"))
        self.assertEqual("refs/heads/main", git(copied, "symbolic-ref", "HEAD"))
        self.assertEqual("refs/remotes/origin/main", git(copied, "symbolic-ref", "refs/remotes/origin/HEAD"))
        self.assertIn(
            "  Default branch:          main (up to date; switched to it; origin/HEAD named topic/old, which origin "
            "lacks: refreshed it from origin)\n",
            self.summary(lines) + "\n",
        )
        self.assertEqual(stale, git(copied, "rev-parse", "refs/heads/topic/old"), "its own work is kept")

    def test_a_dangling_origin_head_origin_will_not_refresh_falls_back_to_main(self) -> None:
        bare_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/unborn")
        git(self.clone, "switch", "--quiet", "-c", "topic")
        git(self.clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/renamed")

        code, lines = self.sweep(None, "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[self.clone.as_posix(), "cleaned"]], facts(lines, "REPO"), "a fallback is never quiet")
        self.assertEqual("refs/heads/main", git(self.clone, "symbolic-ref", "HEAD"))
        default = next(line for line in self.summary(lines).splitlines() if "Default branch:" in line)
        prefix = (
            "  Default branch:          main (up to date; switched to it; origin/HEAD names renamed, which origin "
            "lacks: fell back to main — refreshing it failed: "
        )
        self.assertTrue(default.startswith(prefix), default)

    def test_a_repository_whose_default_branch_cannot_be_found_is_left_on_its_branch(self) -> None:
        bare_git(self.remote, "branch", "-m", "main", "trunk")
        bare_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/unborn")
        git(self.clone, "switch", "--quiet", "-c", "topic")

        code, lines = self.sweep(None, "my repo")
        self.assertEqual(1, code, lines)
        self.assertEqual([[self.clone.as_posix(), "error"]], facts(lines, "REPO"))
        errors = facts(lines, "ERROR")
        self.assertEqual(1, len(errors), lines)
        self.assertTrue(errors[0][0].startswith("cannot determine default branch: origin/HEAD names main, which "))
        self.assertTrue(errors[0][0].endswith("and origin has no main or master; left on topic"), errors)
        self.assertEqual("refs/heads/topic", git(self.clone, "symbolic-ref", "HEAD"))
        self.assertEqual([], facts(lines, "PLAN"))

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
        with (
            mock.patch.object(rc, "new_plans_directory", NEW_PLANS_DIRECTORY),
            mock.patch.object(tempfile, "tempdir", str(temporary)),
            contextlib.redirect_stdout(output),
        ):
            code = rc.main(["sweep", "--repos-root", str(self.repos), "my repo"], rc.Services(gh=self.github))
        lines = output.getvalue().splitlines()
        self.assertEqual(0, code, lines)
        self.assertEqual("PLANS", lines[0].split("\t")[0], "the directory comes first")
        plans = Path(facts(lines, "PLANS")[0][0])
        self.assertEqual(temporary, plans.parent)
        self.assertTrue(plans.name.startswith("repo-cleanup-plans-"), plans)
        self.assertEqual([[(plans / "my repo.json").as_posix()]], facts(lines, "PLAN"))
        self.assertTrue((plans / "my repo.json").is_file())

    def test_the_plans_directory_a_fixture_sweep_makes_is_used_and_printed(self) -> None:
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
            elif line.split("\t")[0] not in ("SWEPT", "QUIET", "PROCESSED"):
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
            {"PLANS", "POLICY", "REPO", "PLAN", "SWEPT", "SUMMARY", "DIRTY_MAIN", "FETCH_FAILED", "CONFIRM_LOCAL"},
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

    def summary(self, block: list[str]) -> str:
        return "\n".join(fields[0] for fields in facts(block, "SUMMARY"))

    def test_a_branch_deletion_git_refuses_is_reported_and_the_sweep_exits_1(self) -> None:
        self.finished_branch("done")
        self.finished_branch("done-tree")
        tree = self.area / "done-tree wt"
        git(self.clone, "worktree", "add", "--quiet", str(tree), "done-tree")
        services = rc.Services(git=refusing_branch_deletion("done", "done-tree"), gh=self.github)
        code, lines = self.sweep(services)
        self.assertEqual(1, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(lines, "REPO"))
        summary = self.summary(lines)
        for name in ("done", "done-tree"):
            self.assertIn(f"{name}: error: cannot lock ref 'refs/heads/{name}'", summary)
            self.assertIsNotNone(self.tip(name), name)
        self.assertIn("Worktrees removed:       1 — ", summary)
        self.assertFalse(tree.exists(), "the worktree removal Git accepted still happens")

    def test_a_sweep_whose_only_refused_deletion_is_a_removed_worktrees_branch_exits_1(self) -> None:
        self.finished_branch("done-tree")
        tree = self.area / "done-tree wt"
        git(self.clone, "worktree", "add", "--quiet", str(tree), "done-tree")
        services = rc.Services(git=refusing_branch_deletion("done-tree"), gh=self.github)
        code, lines = self.sweep(services)
        self.assertEqual(1, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(lines, "REPO"))
        self.assertIn("done-tree: error: cannot lock ref 'refs/heads/done-tree'", self.summary(lines))
        self.assertIsNotNone(self.tip("done-tree"))
        self.assertFalse(tree.exists(), "the worktree removal Git accepted still happens")

    def test_a_failure_partway_through_apply_reports_what_was_done_before_it(self) -> None:
        self.finished_branch("done")
        self.push_branch("behind")
        self.advance_remotely("behind")

        def failing(command: Sequence[str], timeout: float) -> GitResult:
            if "update-ref" in command and "refs/heads/behind" in command:
                raise GitError("git vanished", kind="execution")
            return git_client.subprocess_runner(command, timeout)

        code, lines = self.sweep(rc.Services(git=GitClient(failing), gh=self.github), "my repo")
        self.assertEqual(1, code, lines)
        plan = (self.root / "plans" / "my repo.json").as_posix()
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "git-failed"]], facts(lines, "REPO"))
        block = self.blocks(lines)["my repo"]
        self.assertEqual(["PLAN", plan], block[0].split("\t"), "the plan stays usable for confirmations")
        self.assertEqual("ERROR\tcould not run git: git vanished", block[-1], "the failure follows the summary")
        summary = self.summary(block)
        self.assertTrue(
            summary.startswith("my repo cleanup stopped partway (could not run git: git vanished); done before it:"),
            summary,
        )
        self.assertIn("  Branches deleted:        1 — done", summary)
        self.assertIsNone(self.tip("done"))
        recorded = json.loads(Path(plan).read_text(encoding="utf-8"))
        self.assertEqual((True, "could not run git: git vanished"), (recorded["applied"], recorded["stopped"]))

    def test_a_later_command_that_completes_no_longer_reports_the_stopped_cleanup(self) -> None:
        git(self.clone, "branch", "scratch", "main")
        self.push_branch("behind")
        self.advance_remotely("behind")

        def failing(command: Sequence[str], timeout: float) -> GitResult:
            if "update-ref" in command and "refs/heads/behind" in command:
                raise GitError("git vanished", kind="execution")
            return git_client.subprocess_runner(command, timeout)

        self.sweep(rc.Services(git=GitClient(failing), gh=self.github), "my repo")
        plan = (self.root / "plans" / "my repo.json").as_posix()
        stopped = "my repo cleanup stopped partway (could not run git: git vanished); done before it:"
        code, lines = self.report()
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "git-failed"]], facts(lines, "REPO"))
        self.assertEqual(stopped, self.summary(lines).splitlines()[0])
        self.assertEqual([], facts(lines, "ERROR"), "the summary's heading names the failure")
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error."]],
            facts(lines, "PROCESSED"),
        )

        code, lines = self.invoke("delete-local", "--plan", plan, "--branch", "scratch")
        self.assertEqual((0, [["scratch"]]), (code, facts(lines, "DELETED")), lines)
        self.assertEqual("my repo cleanup complete:", self.summary(lines).splitlines()[0])
        self.assertNotIn("stopped", json.loads(Path(plan).read_text(encoding="utf-8")))
        code, reported = self.report()
        self.assertEqual(0, code, reported)
        self.assertEqual("POLICY", reported[0].split("\t")[0], "the lists the sweep used come first")
        summary = [line for line in lines if line.startswith("SUMMARY\t")]
        self.assertEqual(summary, reported[2:-1], "the report shows the last summary printed")
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(reported, "PROCESSED"),
            "a stop a later command completed is not outstanding",
        )

    def report(self, *aborted: str, plans: Path | None = None) -> tuple[int, list[str]]:
        arguments = ["report", "--record", str(plans or self.root / "plans")]
        for repository in aborted:
            arguments += ["--aborted", repository]
        return self.invoke(*arguments)

    def test_the_report_gives_each_repositorys_last_summary_and_counts_an_aborted_and_a_continued_dirty_one(
        self,
    ) -> None:
        self.finished_branch("done")
        git(self.clone, "branch", "scratch", "main")
        aborted = self.make_clone("aborted repo")
        (aborted / "draft.txt").write_text("draft\n", encoding="utf-8")
        broken = self.make_clone("broken repo")
        git(broken, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(broken, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")
        continued = self.make_clone("continued repo")
        git(continued, "branch", "kept", "main")
        (continued / "draft.txt").write_text("draft\n", encoding="utf-8")
        self.make_clone("quiet repo")
        self.make_clone("second quiet repo")

        code, lines = self.sweep()
        self.assertEqual(1, code, lines)
        root = self.repos.as_posix()
        self.assertEqual(
            [["6", "cleaned=1", "quiet=2", "dirty=2", "fetch-failed=1", "error=0", "git-failed=0"]],
            facts(lines, "SWEPT"),
        )
        code, lines = self.report()
        self.assertEqual(1, code, lines)
        self.assertEqual(
            [
                [
                    f"still dirty: {root}/aborted repo, {root}/continued repo; sweep each again with --skip-checkout "
                    "and the same --record, or name it with --aborted"
                ]
            ],
            facts(lines, "FAILED"),
        )

        code, lines = self.invoke(
            "sweep",
            "--repos-root",
            str(self.repos),
            "--record",
            str(self.root / "plans"),
            "--skip-checkout",
            "continued repo",
        )
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{root}/continued repo", "cleaned"]], facts(lines, "REPO"))
        self.assertEqual([[(self.root / "plans").as_posix()]], facts(lines, "PLANS"), "one directory holds every plan")
        _, confirmed = self.invoke(
            "delete-local", "--plan", (self.root / "plans" / "my repo.json").as_posix(), "--branch", "scratch"
        )
        self.assertEqual([["scratch"]], facts(confirmed, "DELETED"), confirmed)

        code, lines = self.report(f"{root}/aborted repo")
        self.assertEqual(0, code, lines)
        self.assertEqual(
            [
                [f"{root}/aborted repo", "aborted"],
                [f"{root}/broken repo", "fetch-failed"],
                [f"{root}/continued repo", "cleaned"],
                [f"{root}/my repo", "cleaned"],
            ],
            facts(lines, "REPO"),
        )
        self.assertEqual([[f"{root}/quiet repo", f"{root}/second quiet repo"]], facts(lines, "QUIET"))
        self.assertEqual(
            [["Processed 6 repositories; skipped: 1 fetch failed, 1 aborted, 0 with an error."]],
            facts(lines, "PROCESSED"),
        )
        self.assertEqual("PROCESSED", lines[-1].split("\t")[0])
        blocks = self.blocks(lines)
        self.assertEqual([], blocks["aborted repo"])
        self.assertTrue(facts(blocks["broken repo"], "SUMMARY")[0][0].startswith("broken repo skipped: fetch failed"))
        self.assertEqual(
            [line for line in confirmed if line.startswith("SUMMARY	")],
            blocks["my repo"],
            "the summary delete-local printed last, as given",
        )
        self.assertIn("Local-only (kept):       1 — kept", self.summary(blocks["continued repo"]))

    def test_the_report_refuses_an_aborted_repository_the_sweep_did_not_leave_dirty(self) -> None:
        self.sweep(None, "my repo")
        for named in (f"{self.repos.as_posix()}/my repo", f"{self.repos.as_posix()}/elsewhere"):
            with self.subTest(named=named):
                code, lines = self.report(named)
                self.assertEqual(1, code, lines)
                self.assertEqual(
                    [[f"{named} was not left dirty by a sweep of this directory; name only a repository you aborted"]],
                    facts(lines, "FAILED"),
                )
                self.assertEqual(1, len(lines), lines)

    def test_the_report_reads_an_error_the_sweep_recorded_for_a_repository_with_no_plan(self) -> None:
        self.make_clone("other repo")

        def failing(
            root: str,
            repos_root: str,
            services: rc.Services,
            chosen: rc.DefaultBranch | None = None,
            policy: cleanup_policy.Policy | None = None,
        ) -> dict[str, Any]:
            raise ValueError("not enough values to unpack (expected 4, got 1)")

        with mock.patch.object(rc, "build_plan", failing):
            self.sweep(None, "other repo")
        code, lines = self.report()
        self.assertEqual(0, code, lines)
        self.assertEqual(
            [
                f"POLICY\tdefaults\t{cleanup_policy.POLICY_FILE.as_posix()}",
                f"REPO\t{self.repos.as_posix()}/other repo\terror",
                "ERROR\tunexpected ValueError: not enough values to unpack (expected 4, got 1)",
                "PROCESSED\tProcessed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error.",
            ],
            lines,
        )

    def test_a_sweep_adds_only_to_a_directory_an_earlier_sweep_recorded_its_repositories_in(self) -> None:
        before = sorted(path.name for path in self.root.iterdir())
        for plans in (self.root / "missing", self.root):
            with self.subTest(plans=plans):
                code, lines = self.invoke("sweep", "--repos-root", str(self.repos), "--record", str(plans), "my repo")
                self.assertEqual(
                    (1, [f"FAILED\t{plans} holds no sweep record; name the directory a sweep printed as PLANS"]),
                    (code, lines),
                )
        self.assertEqual([], self.github.calls, "refused before anything ran")
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()), "nothing was created")

    def test_the_report_refuses_a_directory_no_sweep_wrote(self) -> None:
        for plans in (self.root / "missing", self.root):
            with self.subTest(plans=plans):
                code, lines = self.report(plans=plans)
                self.assertEqual(1, code, lines)
                self.assertEqual(
                    [[f"{plans} holds no sweep record; name the directory a sweep printed as PLANS"]],
                    facts(lines, "FAILED"),
                )
        record = self.root / "plans" / rc.SWEEP_RECORD
        rc.save_plan(record, {"schema_version": 1, "repositories": [{"root": 3}]})
        code, lines = self.report()
        self.assertEqual((1, [[f"{record} is not a repo-cleanup sweep record"]]), (code, facts(lines, "FAILED")), lines)

    def test_a_repository_is_quiet_only_when_nothing_changed(self) -> None:
        broken = self.make_clone("broken repo")
        git(broken, "switch", "--quiet", "-c", "topic")
        git(broken, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(broken, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")
        self.make_clone("forwarded repo")
        git(self.clone, "switch", "--quiet", "-c", "topic")
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        self.make_clone("quiet repo")
        git(self.make_clone("switched repo"), "switch", "--quiet", "--detach")
        stuck = self.make_clone("stuck repo")
        git(stuck, "switch", "--quiet", "--detach")
        git(stuck, "worktree", "add", "--quiet", str(self.root / "stuck main"), "main")

        code, lines = self.sweep()
        self.assertEqual(1, code, lines)
        root = self.repos.as_posix()
        self.assertEqual(
            [
                [f"{root}/broken repo", "fetch-failed"],
                [f"{root}/forwarded repo", "cleaned"],
                [f"{root}/my repo", "cleaned"],
                [f"{root}/quiet repo", "quiet"],
                [f"{root}/stuck repo", "cleaned"],
                [f"{root}/switched repo", "cleaned"],
            ],
            facts(lines, "REPO"),
        )
        blocks = self.blocks(lines)
        self.assertEqual([], facts(blocks["broken repo"], "CHECKOUT"), "a failed fetch stops before the switch")
        self.assertEqual("refs/heads/topic", git(broken, "symbolic-ref", "HEAD"))
        default = "  Default branch:          main"
        self.assertIn(f"{default} (up to date; fast-forwarded)\n", self.summary(blocks["forwarded repo"]))
        self.assertIn(f"{default} (up to date; fast-forwarded; switched to it)\n", self.summary(blocks["my repo"]))
        self.assertIn(f"{default} (up to date; switched to it)\n", self.summary(blocks["switched repo"]))
        self.assertEqual("failed", facts(blocks["stuck repo"], "CHECKOUT")[0][0])
        self.assertIn(f"{default} (up to date; switch to it failed)\n", self.summary(blocks["stuck repo"]))
        for name in ("forwarded repo", "my repo", "quiet repo", "switched repo"):
            self.assertEqual([], facts(blocks[name], "CHECKOUT") + facts(blocks[name], "FF_DEFAULT"), name)

    def test_a_sweep_passes_on_detached_worktrees_and_orphan_directories_and_is_not_quiet(self) -> None:
        detached = self.area / "detached wt"
        git(self.clone, "worktree", "add", "--quiet", "--detach", str(detached), "main")
        (self.area / "leftover").mkdir()
        code, lines = self.sweep(None, str(self.clone))
        self.assertEqual(0, code, lines)
        self.assertEqual([[self.clone.as_posix(), "cleaned"]], facts(lines, "REPO"))
        self.assertEqual(
            [git(self.clone, "rev-parse", "main")], [fields[1] for fields in facts(lines, "CONFIRM_DETACHED")]
        )
        self.assertTrue(rc.same_path(facts(lines, "ORPHAN")[0][0], str(self.area / "leftover")), lines)

    def test_a_default_branch_not_fast_forwarded_for_changes_is_listed_as_skipped(self) -> None:
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        code, lines = self.sweep(None, "--skip-checkout", "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(lines, "REPO"))
        held = f"worktree {self.clone.as_posix()} has 1 uncommitted change"
        self.assertIn(f"  Fast-forward skipped:    1 — main (behind 1; {held})", self.summary(lines))

    def test_a_branch_name_that_is_not_utf_8_is_reported_with_a_replacement_character(self) -> None:
        sha = git(self.clone, "rev-parse", "main")
        # Windows cannot name a loose ref file with these bytes, so the ref goes in packed-refs, whose header then
        # stops claiming sorted order.
        packed = self.clone / ".git" / "packed-refs"
        existing = packed.read_bytes() if packed.exists() else b""
        packed.write_bytes(existing.replace(b" sorted", b"") + f"{sha} refs/heads/caf".encode() + b"\xe9\n")
        code, lines = self.sweep(None, "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([], [line for line in lines if re.search("[\udc80-\udcff]", line)], "all printable as UTF-8")
        if sys.platform == "win32":
            # gh and git are given the name Python decoded, which matches no ref, so the branch is kept unverified.
            self.assertIn("  PR status unverified:    1 — caf� (", self.summary(lines))
        else:
            # Elsewhere an argument carries the decoded name back as Git's own bytes, so git finds the ref, a
            # branch that was never pushed, but a confirmation could only name it by the replacement character.
            self.assertIn(f"  Preserved:               1 — caf�: {UNNAMEABLE.format('caf�')}", self.summary(lines))
        self.assertEqual([], facts(lines, "CONFIRM_LOCAL"), "a name that does not survive the line is never offered")
        plan = json.loads((self.root / "plans" / "my repo.json").read_text(encoding="utf-8"))
        self.assertEqual(["caf\udce9"], [branch["name"] for branch in plan["branches"]], "the plan keeps Git's bytes")

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
        block = self.blocks(lines)["my repo"]
        self.assertEqual([], facts(block, "CONFIRM_LOCAL"), "a confirmation could not name it")
        self.assertIn(f"  Preserved:               1 — odd name: {UNNAMEABLE.format('odd name')}", self.summary(block))
        self.assertTrue(all("\u2028" not in line for line in lines), "every fact stays on one line")
        plan = json.loads((self.root / "plans" / "my repo.json").read_text(encoding="utf-8"))
        self.assertEqual([name], [branch["name"] for branch in plan["branches"]])

    def test_an_unexpected_failure_reports_that_repository_and_every_other_then_exits_1(self) -> None:
        self.finished_branch("done")
        self.make_clone("other repo")
        build_plan = rc.build_plan

        def failing(
            root: str,
            repos_root: str,
            services: rc.Services,
            chosen: rc.DefaultBranch | None = None,
            policy: cleanup_policy.Policy | None = None,
        ) -> dict[str, Any]:
            if root.endswith("other repo"):
                raise ValueError("not enough values to unpack (expected 4, got 1)")
            return build_plan(root, repos_root, services, chosen, policy)

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

    def test_a_sweep_that_cannot_start_fails_with_one_line_and_creates_nothing(self) -> None:
        self.github.authenticated = False
        code, lines = self.sweep()
        self.assertEqual(1, code, lines)
        self.assertEqual(["FAILED\tGitHub CLI not authenticated — run 'gh auth login'"], lines)
        self.assertFalse((self.root / "plans").exists())
        temporary = self.root / "tmp"
        temporary.mkdir()
        output = io.StringIO()
        with mock.patch.object(tempfile, "tempdir", str(temporary)), contextlib.redirect_stdout(output):
            code = rc.main(["sweep", "--repos-root", str(self.repos)], rc.Services(gh=self.github))
        self.assertEqual((1, ["FAILED"]), (code, [line.split("\t")[0] for line in output.getvalue().splitlines()]))
        self.assertEqual([], list(temporary.iterdir()), "no plans directory is created before the access check")

    def test_only_the_commands_the_skill_runs_remain(self) -> None:
        for command in ("discover", "sync", "plan", "apply"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    rc.main([command], rc.Services(gh=self.github))
                self.assertEqual(2, raised.exception.code)


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
        with self.assertRaisesRegex(rc.CleanupError, "^git symbolic-ref failed in .*bad config line 1"):
            rc.default_branch(rc.Services(), root)

    def test_a_broken_ref_is_named_not_taken_for_no_commit(self) -> None:
        root = self.repository("topic")
        broken = root / ".git" / "refs" / "heads" / "broken"
        broken.write_text("nonsense\n", encoding="utf-8")
        with self.assertRaisesRegex(
            rc.CleanupError, "^git rev-parse failed in .*ignoring broken ref refs/heads/broken"
        ):
            rc.branch_tip(rc.Services(), root, "broken")
        self.assertIsNone(rc.branch_tip(rc.Services(), root, "missing"))
        self.assertEqual(git(root, "rev-parse", "HEAD"), rc.branch_tip(rc.Services(), root, "topic"))

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


class WorktreeTestCase(CleanupTestCase):
    """A repository whose worktrees a test adds, fills, and removes as apply removes a stale one."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="repo cleanup remove ")).resolve()
        self.addCleanup(remove_tree, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "-b", "main")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "initial")
        self.plan: dict[str, Any] = {
            "repo_root": str(self.repo),
            "default": {"name": "main"},
            "policy": {"regenerable": "", "protected": ""},
            "events": [],
        }

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

    def remove(self, name: str, pr: str = "CLOSED") -> tuple[rc.Removal, list[list[str]], Path]:
        path = self.root / f"{name} wt"
        entry = {"path": str(path), "branch": name}
        tip = self.tip(name)
        if tip is None:
            self.fail(f"{name} was added with a branch")
        with contextlib.redirect_stdout(io.StringIO()):
            removed = rc.remove_worktree(rc.Services(), self.plan, entry, tip, pr)
        return removed, self.plan["events"], path

    def ignore(self, *patterns: str) -> None:
        (self.repo / ".gitignore").write_text("".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "--quiet", "-m", "ignore paths")

    def fill(self, path: Path, *files: str) -> None:
        """Write each file, making its directories, so every ignored path exists in the worktree."""
        for name in files:
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text("x", encoding="utf-8")
        self.assertEqual("", git(path, "status", "--porcelain"), "Git calls it clean")


class RemoveWorktreeTests(WorktreeTestCase):
    """git worktree remove, then git branch -d, as apply runs them for a stale worktree."""

    def test_a_merged_worktree_and_its_branch_are_removed(self) -> None:
        self.add("merged")
        removed, events, path = self.remove("merged")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual([["REMOVED", str(path), "merged"], ["DELETED", "merged"]], events)
        self.assertFalse(path.exists())
        self.assertIsNone(self.tip("merged"))

    def test_a_closed_worktree_is_removed_and_its_branch_offered_instead_of_deleted(self) -> None:
        path = self.add("closed")
        tip = git(self.repo, "rev-parse", "closed")
        with contextlib.redirect_stdout(io.StringIO()):
            removed = rc.remove_worktree(
                rc.Services(), self.plan, {"path": str(path), "branch": "closed"}, tip, "CLOSED", ask=True
            )
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual([["REMOVED", str(path), "closed"], ["CONFIRM_LOCAL", "closed"]], self.plan["events"])
        self.assertFalse(path.exists())
        self.assertEqual(tip, self.tip("closed"))

    def test_a_locked_worktree_is_kept_with_gits_reason(self) -> None:
        path = self.add("locked")
        (path / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        git(path, "add", "sentinel.txt")
        git(path, "commit", "--quiet", "-m", "sentinel")
        git(self.repo, "worktree", "lock", str(path))
        removed, events, _ = self.remove("locked")
        self.assertEqual(rc.Removal(removed=False, refused=True), removed)
        self.assertEqual(["PRESERVED", "locked"], events[0][:2])
        self.assertTrue(events[0][2].startswith(f"Git refused to remove worktree {path}: "), events)
        self.assertTrue((path / "sentinel.txt").is_file())
        self.assertIsNotNone(self.tip("locked"))

    def test_untracked_work_is_kept(self) -> None:
        path = self.add("untracked")
        (path / "draft.txt").write_text("draft\n", encoding="utf-8")
        removed, events, _ = self.remove("untracked")
        self.assertEqual(rc.Removal(removed=False, refused=True), removed)
        self.assertEqual(["PRESERVED", "untracked"], events[0][:2])
        self.assertTrue((path / "draft.txt").is_file())

    def test_untracked_work_is_kept_when_the_user_hides_untracked_files(self) -> None:
        # The setting hides untracked files from git status, and from the check git worktree remove makes itself.
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        path = self.add("hidden")
        (path / "draft.txt").write_text("draft\n", encoding="utf-8")
        self.assertEqual("", git(path, "status", "--porcelain"), "the setting hides the draft")
        self.assertEqual(1, rc.dirty_count(rc.Services(), path))
        removed, events, _ = self.remove("hidden")
        self.assertEqual(rc.Removal(removed=False, refused=True), removed)
        self.assertEqual(["PRESERVED", "hidden"], events[0][:2])
        self.assertTrue(events[0][2].startswith(f"Git refused to remove worktree {path}: "), events)
        self.assertTrue((path / "draft.txt").is_file())

    def test_ignored_files_keep_a_worktree_when_the_user_hides_untracked_files(self) -> None:
        self.ignore(".env")
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        path = self.add("hidden")
        self.fill(path, ".env")
        removed, events, _ = self.remove("hidden")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed, "ignored files keep it")
        self.assertEqual(
            [["PRESERVED", "hidden", f"worktree {path} holds 1 ignored path git worktree remove would delete: .env"]],
            events,
        )
        self.assertTrue((path / ".env").is_file())

    def test_ignored_files_keep_a_worktree_git_would_remove_with_them(self) -> None:
        # git worktree remove counts a worktree holding only ignored files as clean and deletes them.
        self.ignore(".env", "bin/")
        path = self.add("ignored")
        self.fill(path, ".env", "bin/app.dll")
        removed, events, _ = self.remove("ignored")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed, "ignored files keep it")
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "ignored",
                    f"worktree {path} holds 2 ignored paths git worktree remove would delete: .env, bin/",
                ]
            ],
            events,
        )
        self.assertTrue((path / ".env").is_file())
        self.assertIsNotNone(self.tip("ignored"))

    def test_a_worktree_holding_only_regenerable_caches_is_removed_with_them(self) -> None:
        self.ignore("__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "node_modules/.cache/")
        path = self.add("cached")
        self.fill(
            path,
            "__pycache__/a.cpython-311.pyc",
            "pkg/__pycache__/b.cpython-311.pyc",
            ".pytest_cache/v/cache/nodeids",
            ".mypy_cache/3.11/a.json",
            ".ruff_cache/0.1/x",
            "node_modules/.cache/babel/y",
        )
        removed, events, _ = self.remove("cached")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual(
            [
                [
                    "REMOVED",
                    str(path),
                    "cached",
                    "deleted 6 regenerable paths: .mypy_cache/, .pytest_cache/, .ruff_cache/, __pycache__/, "
                    "node_modules/.cache/, and 1 more",
                ],
                ["DELETED", "cached"],
            ],
            events,
        )
        self.assertFalse(path.exists())
        self.assertIsNone(self.tip("cached"))

    def test_any_other_ignored_path_beside_caches_keeps_the_worktree_and_both_are_named(self) -> None:
        self.ignore("__pycache__/", ".pytest_cache/", "node_modules/", ".env", "*.pyc")
        path = self.add("mixed")
        # node_modules as a whole is not a cache: its .cache is hidden under it, and the packages are the user's.
        self.fill(path, "__pycache__/a.pyc", ".pytest_cache/v/n", "node_modules/.cache/y", "node_modules/left-pad/i.js")
        removed, events, _ = self.remove("mixed")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed)
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "mixed",
                    f"worktree {path} holds 1 ignored path git worktree remove would delete: node_modules/; "
                    "also 2 regenerable paths: .pytest_cache/, __pycache__/",
                ]
            ],
            events,
        )
        self.assertTrue((path / "node_modules" / "left-pad" / "i.js").is_file())
        shutil.rmtree(path / "node_modules")
        # A file ignored by its suffix outside a cache is the user's too, however cache-like its name.
        self.fill(path, ".env", "pkg/__pycache__/c.pyc", "pkg/stray.pyc")
        self.plan["events"].clear()
        removed, events, _ = self.remove("mixed")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed)
        self.assertEqual(
            f"worktree {path} holds 2 ignored paths git worktree remove would delete: .env, pkg/stray.pyc; "
            "also 3 regenerable paths: .pytest_cache/, __pycache__/, pkg/__pycache__/",
            events[0][2],
        )
        self.assertIsNotNone(self.tip("mixed"))

    def test_a_preserved_line_names_the_first_ignored_paths_and_counts_the_rest(self) -> None:
        self.ignore("out*/", "*.log")
        path = self.add("flooded")
        self.fill(path, *(f"out{index}/built" for index in range(6)), "a.log", "b.log")
        removed, events, _ = self.remove("flooded")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed)
        self.assertEqual(
            f"worktree {path} holds 8 ignored paths git worktree remove would delete: "
            "a.log, b.log, out0/, out1/, out2/, and 3 more",
            events[0][2],
        )
        self.assertEqual(rc.NAMED_PATHS, 5)

    def test_an_ignored_path_is_regenerable_inside_a_shipped_directory_with_its_project_beside_it(self) -> None:
        # test_cleanup_policy.py holds the shipped list itself.
        shipped = cleanup_policy.configured()
        tree = self.root / "layout"
        for name in ("App/App.csproj", "Lib/Lib.fsproj", "web/package.json"):
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text("x", encoding="utf-8")
        for name in (
            "__pycache__",
            "pkg/__pycache__",
            "node_modules/.cache",
            "node_modules/left-pad",
            "web/node_modules/left-pad",
            ".vs/App",
            "App/bin/Debug",
            "App/obj",
            "Lib/obj",
            "tools/bin",
            "Lib/sub/bin",
            ".cache",
            "my__pycache__",
            "__pycache__.bak",
            "pytest_cache",
        ):
            (tree / name).mkdir(parents=True, exist_ok=True)
        for entry, regenerable in (
            ("__pycache__/", True),
            ("pkg/__pycache__/", True),
            ("pkg/__pycache__/a.pyc", True),
            ("node_modules/.cache/", True),
            ("web/node_modules/.cache/babel/", True),
            ("web/node_modules/", True),
            ("web/node_modules/left-pad/", True),
            (".vs/", True),
            (".vs/App/v17/.suo", True),
            ("App/bin/", True),
            ("App/bin/Debug/App.dll", True),
            ("App/obj/", True),
            ("Lib/obj/", True),
            ("node_modules/", False),
            ("node_modules/left-pad/", False),
            ("tools/bin/", False),
            ("Lib/sub/bin/", False),
            ("bin/", False),
            (".cache/", False),
            ("my__pycache__/", False),
            ("__pycache__.bak/", False),
            (".env", False),
            ("pytest_cache/", False),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(regenerable, is_regenerable(entry, tree, shipped))

    def test_a_projects_build_and_ide_output_is_removed_with_its_worktree_and_a_bin_without_one_keeps_it(self) -> None:
        for name in ("App/App.csproj", "web/package.json"):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text("<Project />\n", encoding="utf-8")
        git(self.repo, "add", "App/App.csproj", "web/package.json")
        self.ignore("bin/", "obj/", ".vs/", "node_modules/")
        built = self.add("built")
        self.fill(
            built,
            "App/bin/Debug/App.dll",
            "App/obj/project.assets.json",
            ".vs/App/v17/.suo",
            "web/node_modules/left-pad/index.js",
        )
        removed, events, _ = self.remove("built", "MERGED")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual(
            [
                [
                    "REMOVED",
                    str(built),
                    "built",
                    "deleted 4 regenerable paths: .vs/, App/bin/, App/obj/, web/node_modules/",
                ],
                ["DELETED", "built"],
            ],
            events,
        )
        self.assertFalse(built.exists())
        tools = self.add("tools")
        self.fill(tools, "tools/bin/run.sh", "App/bin/Debug/App.dll")
        self.plan["events"].clear()
        removed, events, _ = self.remove("tools", "MERGED")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed, "a bin with no project beside it")
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "tools",
                    f"worktree {tools} holds 1 ignored path git worktree remove would delete: tools/bin/; also 1 "
                    "regenerable path: App/bin/",
                ]
            ],
            events,
        )
        self.assertTrue((tools / "tools" / "bin" / "run.sh").is_file())

    def test_a_worktree_whose_folder_git_could_not_delete_says_its_record_and_files_are_gone(self) -> None:
        path = self.add("held")

        def held_folder(command: Sequence[str], timeout: float) -> GitResult:
            result = git_client.subprocess_runner(command, timeout)
            if "worktree" in command and "remove" in command:
                # As Windows leaves it when a process works in the folder: Git deleted the files and the record,
                # then failed on the folder itself.
                Path(command[-1]).mkdir()
                return GitResult(255, "", f"error: failed to delete '{command[-1]}': Permission denied")
            return result

        tip = self.tip("held") or ""
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = rc.remove_worktree(
                rc.Services(git=GitClient(held_folder)), self.plan, {"path": str(path), "branch": "held"}, tip, "MERGED"
            )
        self.assertEqual(rc.Removal(removed=False, refused=True), outcome)
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "held",
                    f"Git deleted worktree {path}'s record and files but not its folder: error: failed to delete "
                    f"'{path}': Permission denied; a sweep offers what is left there as a directory Git no longer "
                    "tracks",
                ]
            ],
            self.plan["events"],
        )
        self.assertNotIn(str(path), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(tip, self.tip("held"))

    def test_edits_hidden_from_status_keep_a_worktree_asked_just_before_its_removal(self) -> None:
        (self.repo / "settings.json").write_text('{"local": false}\n', encoding="utf-8")
        git(self.repo, "add", "settings.json")
        git(self.repo, "commit", "--quiet", "-m", "settings")
        for name, bit in (("skipped", "--skip-worktree"), ("assumed", "--assume-unchanged")):
            with self.subTest(bit=bit):
                path = self.add(name)
                (path / "settings.json").write_text('{"local": true}\n', encoding="utf-8")
                git(path, "update-index", bit, "settings.json")
                self.assertEqual("", git(path, "status", "--porcelain"), "the bit hides the edit from status")
                self.plan["events"].clear()
                removed, events, _ = self.remove(name, "MERGED")
                self.assertEqual(rc.Removal(removed=False, refused=False), removed)
                self.assertEqual(
                    [
                        [
                            "PRESERVED",
                            name,
                            f"worktree {path} holds edits status does not show, to 1 file marked skip-worktree or "
                            "assume-unchanged: settings.json",
                        ]
                    ],
                    events,
                )
                self.assertEqual('{"local": true}\n', (path / "settings.json").read_text(encoding="utf-8"))
                self.assertIsNotNone(self.tip(name))

    def test_an_unmerged_branch_outlives_its_removed_worktree(self) -> None:
        path = self.add("unmerged")
        git(path, "commit", "--quiet", "--allow-empty", "-m", "work")
        sha = self.tip("unmerged")
        removed, events, _ = self.remove("unmerged")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual([["REMOVED", str(path), "unmerged"], ["UNMERGED", "unmerged", sha]], events)
        self.assertFalse(path.exists())
        self.assertEqual(sha, self.tip("unmerged"))

    def test_a_branch_deletion_git_refuses_after_the_worktree_is_removed_is_reported_as_refused(self) -> None:
        path = self.add("held")
        tip = self.tip("held") or ""
        entry = {"path": str(path), "branch": "held"}
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = rc.remove_worktree(
                rc.Services(git=refusing_branch_deletion("held")), self.plan, entry, tip, "MERGED"
            )
        self.assertEqual(rc.Removal(removed=True, refused=True), outcome)
        self.assertEqual(
            [["REMOVED", str(path), "held"], ["PRESERVED", "held", "error: cannot lock ref 'refs/heads/held'"]],
            self.plan["events"],
        )
        self.assertFalse(path.exists())
        self.assertEqual(tip, self.tip("held"))


class UnitTests(CleanupTestCase):
    def test_the_orphan_confirmation_states_only_what_the_sweep_checked(self) -> None:
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        question = re.search(r'For `ORPHAN <path>` lines, call it with question `"([^`]*)"`', skill)
        if question is None:
            self.fail("the ORPHAN confirmation is not where the skill asks it")
        self.assertEqual(
            "<REPO_NAME>: These directories in the worktree area are no longer Git worktrees, and Git tracks none of"
            " their files. They cannot be recovered once deleted. Delete or keep each?",
            question.group(1),
        )

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
            # In any letter case: Windows treats Release and release as one directory and one loose ref.
            ("Release/4.10", "C:/GitHub/Worktrees/example/topic", True),
            ("RELEASE/4.10", "C:/GitHub/Worktrees/example/topic", True),
            ("topic/fix", "C:/GitHub/Worktrees/example/Release/4.10", True),
            ("topic/fix", "C:\\GitHub\\RELEASE\\example", True),
        ):
            with self.subTest(branch=branch, path=path):
                self.assertEqual(expected, rc.is_protected(branch, path, cleanup_policy.configured()))

    def test_github_remote_urls(self) -> None:
        for url, expected in (
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://user@github.com/Owner/my.repo.git", "Owner/my.repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("https://gitlab.com/owner/repo.git", None),
            ("https://example.invalid/github.com/owner/repo", None),
            ("git@example.invalid:github.com/owner/repo", None),
            ("https://github.com/owner/repo/extra", None),
            ("C:/remotes/repo.git", None),
        ):
            with self.subTest(url=url):
                answer = GitResult(0, f"{url}\n", "")

                def origin(command: Sequence[str], timeout: float, answer: GitResult = answer) -> GitResult:
                    return answer

                services = rc.Services(git=GitClient(origin))
                self.assertEqual(expected, rc.name_with_owner(services, "C:/checkout"))

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

    def test_empty_directory_pruning_stops_below_the_repository_root(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup prune root ")).resolve()
        self.addCleanup(remove_tree, root)
        area, repository = root / "Worktrees", root / "my repo"
        (repository / ".worktrees" / "group").mkdir(parents=True)
        area.mkdir()
        self.assertEqual(
            [(repository / ".worktrees" / "group").as_posix(), (repository / ".worktrees").as_posix()],
            rc.prune_empty_parents(str(repository / ".worktrees" / "group" / "removed"), [str(area), str(repository)]),
        )
        self.assertTrue(repository.is_dir(), "the repository root itself is never removed")
        self.assertTrue(area.is_dir())

    def test_a_rename_is_one_change_and_the_path_it_came_from_never_an_ignored_one(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup status ")).resolve()
        self.addCleanup(remove_tree, root)
        git(root, "init", "--quiet")
        (root / "!! notes.txt").write_text("content\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "--quiet", "-m", "initial")
        git(root, "mv", "!! notes.txt", "notes.txt")
        (root / "untracked.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(2, rc.dirty_count(rc.Services(), root))
        (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (root / "build.log").write_text("log\n", encoding="utf-8")
        self.assertEqual(["build.log"], rc.ignored_entries(rc.Services(), root))


class ConsoleTests(CleanupTestCase):
    def sealed_in(self, root: Path) -> dict[str, str]:
        """The environment of a script run whose temporary directory, and so whose seals, lie under `root`, where
        this test's saves write them too."""
        temporary = root / "temp"
        temporary.mkdir()
        patcher = mock.patch.object(rc, "SEALS", temporary / "repo-cleanup-seals")
        patcher.start()
        self.addCleanup(patcher.stop)
        return {**os.environ, "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary)}

    def test_output_a_cp1252_console_cannot_encode_is_written_as_utf_8(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup console ")).resolve()
        self.addCleanup(remove_tree, root)
        environment = self.sealed_in(root)
        plan = root / "plan → ✓.json"
        rc.save_plan(plan, written_plan(schema_version=0))
        result = subprocess.run(
            [sys.executable, "-B", rc.__file__, "delete-local", "--plan", str(plan), "--branch", "scratch"],
            capture_output=True,
            env={**environment, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(1, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            [f"FAILED\t{plan} is not a repo-cleanup plan: schema_version must be one of 1, found integer 0"],
            result.stdout.decode("utf-8").splitlines(),
        )

    def test_a_branch_name_git_gave_as_bytes_that_are_not_utf_8_is_printed(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup console ")).resolve()
        self.addCleanup(remove_tree, root)
        environment = self.sealed_in(root)
        plan = root / "plan.json"
        rc.save_plan(plan, written_plan(events=[["CONFIRM_LOCAL", "caf\udce9"]]))
        entry = {"root": "/repos/my repo", "state": "cleaned", "plan": str(plan), "lines": []}
        record = {"schema_version": rc.RECORD_SCHEMA_VERSION, "repositories": [entry], "policy": []}
        rc.save_plan(root / rc.SWEEP_RECORD, record)
        result = subprocess.run(
            [sys.executable, "-B", rc.__file__, "report", "--record", str(root)],
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))
        self.assertIn("SUMMARY\t  Local-only (kept):       1 — caf�", result.stdout.decode("utf-8").splitlines())


class PlanVersionTests(CleanupTestCase):
    def test_a_plan_whose_schema_version_is_not_the_integer_one_is_refused(self) -> None:
        # A boolean or an integral float compares equal to the version; only the integer itself is it.
        root = Path(tempfile.mkdtemp(prefix="repo cleanup plan ")).resolve()
        self.addCleanup(remove_tree, root)
        plan = root / "plan.json"
        rc.save_plan(plan, written_plan())
        self.assertEqual(1, rc.load_plan(plan)["schema_version"])
        for version in (True, 1.0, "1", [1], {"version": 1}, None, 0, 2):
            with self.subTest(version=version):
                rc.save_plan(plan, written_plan(schema_version=version))
                with self.assertRaisesRegex(rc.CleanupError, "is not a repo-cleanup plan"):
                    rc.load_plan(plan)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = rc.main(["delete-local", "--plan", str(plan), "--branch", "scratch"], rc.Services())
                lines = out.getvalue().splitlines()
                self.assertEqual(1, code, lines)
                self.assertEqual(1, len(lines), lines)
                self.assertTrue(
                    lines[0].startswith(f"FAILED\t{plan} is not a repo-cleanup plan: schema_version must be one of 1"),
                    lines,
                )

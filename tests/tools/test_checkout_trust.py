"""tools/checkout_trust.py: which checkouts follow another repository's pull request, read from the checkout's git, and
that every tool deploying into a canary home refuses one before it starts anything."""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from checkout_fixture import FOREIGN, follow, make_foreign, own_checkout
from git_client import GitClient, GitError, GitResult, subprocess_runner

from conftest import GitHome
from tools import checkout_trust

REPOSITORY = Path(__file__).resolve().parents[2]

# What reaches a canary home: the deployer's option, and the runtime canary's functions that make a home or deploy.
CANARY_OPTION = "--canary-home"
CANARY_FUNCTIONS = frozenset({"create_home", "deploy"})
CANARY_MODULE = "tools.runtime_canary"


def temporary_root(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name).resolve() / "checkout with spaces"
    root.mkdir()
    return root


class CheckoutTests(unittest.TestCase):
    """A checkout follows another repository's pull request, read from the checkout's git."""

    # The home conftest.py's git_home fixture gives the running test's git.
    git_home: GitHome

    def setUp(self) -> None:
        self.git = GitClient()
        self.root = own_checkout(temporary_root(self), self.git)

    def foreign(self) -> str | None:
        return checkout_trust.foreign_checkout(self.git, self.root)

    def test_a_branch_of_the_user_s_own_is_accepted(self) -> None:
        self.assertIsNone(self.foreign())
        for remote, merge in (("origin", "refs/heads/main"), (".", "refs/heads/other")):
            with self.subTest(remote=remote):
                follow(self.root, self.git, remote, merge)
                self.assertIsNone(self.foreign())

    def test_a_branch_following_a_pull_request_s_ref_or_another_remote_is_refused(self) -> None:
        cases = {
            ("origin", "refs/pull/7/head"): "its branch main follows refs/pull/7/head, a pull request's ref",
            ("someone", "refs/heads/topic"): "its branch main follows someone, not origin",
            ("https://github.com/someone/fork.git", "refs/heads/topic"): (
                "its branch main follows https://github.com/someone/fork.git, not origin"
            ),
        }
        for (remote, merge), reason in cases.items():
            with self.subTest(remote=remote, merge=merge):
                follow(self.root, self.git, remote, merge)
                self.assertEqual(reason, self.foreign())

    def test_each_fixture_case_is_refused_with_its_reason(self) -> None:
        for case, reason in FOREIGN.items():
            with self.subTest(case=case):
                root = own_checkout(temporary_root(self), self.git)
                self.assertEqual(reason, make_foreign(root, self.git, case))
                self.assertEqual(reason, checkout_trust.foreign_checkout(self.git, root))

    def test_a_branch_whose_name_ends_in_a_character_strip_drops_is_named_whole(self) -> None:
        name = "topic\u00a0"
        self.git.output(["checkout", "-q", "-b", name], directory=self.root)
        self.git.output(["config", f"branch.{name}.remote", "someone"], directory=self.root)
        self.git.output(["config", f"branch.{name}.merge", "refs/heads/topic"], directory=self.root)
        self.assertEqual(f"its branch {name} follows someone, not origin", self.foreign())

    def test_a_detached_head_is_accepted_only_at_a_commit_a_branch_of_origin_holds(self) -> None:
        self.git.output(["checkout", "-q", "--detach", "HEAD"], directory=self.root)
        self.assertEqual("its HEAD is detached at a commit no branch of origin holds", self.foreign())
        self.git.output(["checkout", "-q", "--detach", "refs/remotes/origin/main"], directory=self.root)
        self.assertIsNone(self.foreign())

    def test_the_branches_holding_head_are_origin_s_alone_by_their_full_names(self) -> None:
        # origin/topic holds the second commit, origin/main only the first; another remote's branch never counts.
        self.git.output(["update-ref", "refs/remotes/origin/topic", "HEAD"], directory=self.root)
        self.git.output(["update-ref", "refs/remotes/someone/topic", "HEAD"], directory=self.root)
        self.assertEqual(
            ["refs/remotes/origin/topic"], checkout_trust.origin_branches_holding_head(self.git, self.root)
        )
        self.git.output(["checkout", "-q", "--detach", "refs/remotes/origin/main"], directory=self.root)
        self.assertEqual(
            ["refs/remotes/origin/main", "refs/remotes/origin/topic"],
            checkout_trust.origin_branches_holding_head(self.git, self.root),
        )

    def test_a_setting_or_head_git_cannot_read_raises_never_read_as_unset(self) -> None:
        refused = GitResult(1, "", "error: could not lock config file .git/config\n")
        for word in ("branch.main.remote", "branch.main.merge", "symbolic-ref"):

            def run(command: Sequence[str], timeout: float, word: str = word) -> GitResult:
                return refused if word in command else subprocess_runner(command, timeout)

            with self.subTest(word=word), self.assertRaises(GitError) as raised:
                checkout_trust.foreign_checkout(GitClient(run), self.root)
            self.assertIn("could not lock config file", str(raised.exception))

    def test_a_folder_git_cannot_read_raises(self) -> None:
        outside = self.root.parent / "not a checkout"
        outside.mkdir()
        with self.assertRaises(GitError):
            checkout_trust.foreign_checkout(self.git, outside)

    def test_the_reader_reads_the_checkout_it_names_when_called(self) -> None:
        read = checkout_trust.reader(self.root)
        self.assertIsNone(read())
        follow(self.root, self.git, "origin", "refs/pull/7/head")
        self.assertEqual("its branch main follows refs/pull/7/head, a pull request's ref", read())


class RefusalTests(unittest.TestCase):
    def test_a_checkout_of_the_user_s_own_is_not_refused(self) -> None:
        self.assertIsNone(checkout_trust.refusal(lambda: None))

    def test_a_foreign_checkout_is_refused_naming_why(self) -> None:
        self.assertEqual(
            "the checkout follows another repository's pull request: its branch main follows someone, not origin",
            checkout_trust.refusal(lambda: "its branch main follows someone, not origin"),
        )

    def test_a_checkout_git_cannot_read_is_refused_never_read_as_the_user_s_own(self) -> None:
        def unreadable() -> str | None:
            raise GitError("fatal: not a git repository")

        self.assertEqual(
            "cannot read the checkout's branch: fatal: not a git repository", checkout_trust.refusal(unreadable)
        )


def imported_canary_names(tree: ast.Module) -> set[str]:
    """The names a module binds by importing the runtime canary's home-making or deploying functions."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == CANARY_MODULE
        for alias in node.names
        if alias.name in CANARY_FUNCTIONS
    }


def reaches_a_canary_home(tree: ast.Module) -> bool:
    """Whether a module passes --canary-home or uses the runtime canary's function that makes a home or deploys."""
    imported = imported_canary_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == CANARY_OPTION:
            return True
        if isinstance(node, ast.Name) and node.id in imported and not isinstance(node.ctx, ast.Store):
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr in CANARY_FUNCTIONS
            and isinstance(node.value, ast.Name)
            and node.value.id == "runtime_canary"
        ):
            return True
    return False


def refuses_a_foreign_checkout(tree: ast.Module) -> bool:
    """Whether a module calls checkout_trust.refusal."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "refusal"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "checkout_trust"
        for node in ast.walk(tree)
    )


def unguarded_deployers(sources: dict[str, str]) -> list[str]:
    """The modules, by name, that reach a canary home and never call checkout_trust.refusal."""
    unguarded: list[str] = []
    for name, text in sorted(sources.items()):
        tree = ast.parse(text)
        if reaches_a_canary_home(tree) and not refuses_a_foreign_checkout(tree):
            unguarded.append(name)
    return unguarded


class EveryDeployerRefusesTests(unittest.TestCase):
    """Every tool that deploys into a canary home refuses a foreign checkout first, through the one shared refusal."""

    def test_every_tool_reaching_a_canary_home_calls_the_shared_refusal(self) -> None:
        sources = {
            path.relative_to(REPOSITORY).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY / "tools").glob("*.py"))
        }
        self.assertEqual([], unguarded_deployers(sources))
        reaching = {name for name, text in sources.items() if reaches_a_canary_home(ast.parse(text))}
        self.assertTrue(
            {"tools/runtime_canary.py", "tools/plugin_canary.py", "tools/skill_evals.py"} <= reaching, reaching
        )

    def test_a_module_reaching_a_canary_home_without_the_refusal_is_named(self) -> None:
        violating = {
            "option.py": 'ARGUMENTS = ["--canary-home", "home"]\n',
            "imported.py": "from tools.runtime_canary import create_home\n\nhome = create_home()\n",
            "renamed.py": "from tools.runtime_canary import deploy as put\n\nput(home, source)\n",
            "attribute.py": "from tools import runtime_canary\n\nruntime_canary.deploy(home, source)\n",
        }
        self.assertEqual(sorted(violating), unguarded_deployers(violating))

    def test_a_module_that_refuses_first_or_reaches_no_home_is_not_named(self) -> None:
        conforming = {
            "guarded.py": (
                "from tools import checkout_trust, runtime_canary\n\n"
                "if checkout_trust.refusal(checkout_trust.reader(root)) is None:\n"
                "    runtime_canary.deploy(home, source)\n"
            ),
            "unrelated.py": "from tools.runtime_canary import quote\n\ndeploy = quote\n\nprint('--canary-homes')\n",
        }
        self.assertEqual([], unguarded_deployers(conforming))

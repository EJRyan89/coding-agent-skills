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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "validation"))

from checkout_fixture import BRANCHES_REFSPEC, FOREIGN, PULL_REQUESTS_REFSPEC, follow, make_foreign, own_checkout
from git_client import GitClient, GitError, GitResult, subprocess_runner
from validation_support import CallableResolver

from conftest import GitHome
from tools import checkout_trust

REPOSITORY = Path(__file__).resolve().parents[2]

# What reaches a canary home: the deployer's option, and the runtime canary's functions that make a home or deploy.
CANARY_OPTION = "--canary-home"
CANARY_FUNCTIONS = frozenset({"create_home", "start_home", "deploy"})
# A tool imports the runtime canary from the tools package, or by its bare name when tools/ is on sys.path, as running
# a tool's file puts it; the shared refusal likewise.
CANARY_MODULES = ("tools.runtime_canary", "runtime_canary")
CANARY_NAMES = frozenset(f"{module}.{function}" for module in CANARY_MODULES for function in CANARY_FUNCTIONS)
REFUSALS = frozenset({"tools.checkout_trust.refusal", "checkout_trust.refusal"})
DEFINED_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
Function = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


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

    def test_a_detached_head_at_a_mirrored_pull_request_s_head_in_a_clone_is_refused(self) -> None:
        # A fork's pull request reaches origin as refs/pull/7/head; a clone that mirrors pull requests' heads fetches
        # it to refs/remotes/origin/pr/7, which is not a branch of origin however its name reads.
        origin = temporary_root(self)
        self.git.output(["clone", "-q", "--bare", str(self.root), str(origin)])
        self.git.output(["update-ref", "refs/pull/7/head", "refs/heads/main"], directory=origin)
        self.git.output(["update-ref", "refs/heads/main", "refs/heads/main~1"], directory=origin)
        clone = temporary_root(self)
        self.git.output(["clone", "-q", str(origin), str(clone)])
        self.git.output(["config", "--add", "remote.origin.fetch", PULL_REQUESTS_REFSPEC], directory=clone)
        self.git.output(["fetch", "-q", "origin"], directory=clone)
        self.git.output(["checkout", "-q", "--detach", "refs/remotes/origin/pr/7"], directory=clone)
        self.assertEqual([], checkout_trust.origin_branches_holding_head(self.git, clone))
        self.assertEqual(
            "its HEAD is detached at a commit no branch of origin holds",
            checkout_trust.foreign_checkout(self.git, clone),
        )
        self.git.output(["checkout", "-q", "--detach", "refs/remotes/origin/main"], directory=clone)
        self.assertIsNone(checkout_trust.foreign_checkout(self.git, clone))

    def test_a_ref_counts_only_when_origin_s_refspecs_write_it_from_a_branch_alone(self) -> None:
        self.git.output(["update-ref", "refs/remotes/origin/pr/7", "HEAD"], directory=self.root)
        self.git.output(["update-ref", "refs/remotes/origin/topic", "HEAD"], directory=self.root)
        self.git.output(["update-ref", "refs/mirror/heads/side", "HEAD"], directory=self.root)
        cases: dict[tuple[str, ...], list[str]] = {
            # No fetch refspec: nothing is a branch of origin, its name under refs/remotes/origin notwithstanding.
            (): [],
            # The clone's refspec alone writes origin/pr/7 from a branch named pr/7.
            (BRANCHES_REFSPEC,): ["refs/remotes/origin/pr/7", "refs/remotes/origin/topic"],
            # A pull request's refspec may write origin/pr/7 too, so it is never read as a branch.
            (BRANCHES_REFSPEC, PULL_REQUESTS_REFSPEC): ["refs/remotes/origin/topic"],
            (PULL_REQUESTS_REFSPEC,): [],
            # A negative refspec leaves the branch it names out.
            (BRANCHES_REFSPEC, "^refs/heads/topic"): ["refs/remotes/origin/pr/7"],
            # A refspec with no star, and one that writes a branch outside refs/remotes/origin.
            ("refs/heads/topic:refs/remotes/origin/topic", "+refs/heads/*:refs/mirror/heads/*"): [
                "refs/mirror/heads/side",
                "refs/remotes/origin/topic",
            ],
            # A mirror clone's refspec writes every ref from its own name, and only refs/heads/ is a branch.
            ("+refs/*:refs/*",): ["refs/heads/main"],
            # A refspec with no destination writes no local ref.
            ("refs/heads/topic",): [],
        }
        for refspecs, held in cases.items():
            with self.subTest(refspecs=refspecs):
                if self.git.quiet_output(["config", "--get-all", "remote.origin.fetch"], directory=self.root):
                    self.git.output(["config", "--unset-all", "remote.origin.fetch"], directory=self.root)
                for refspec in refspecs:
                    self.git.output(["config", "--add", "remote.origin.fetch", refspec], directory=self.root)
                self.assertEqual(held, checkout_trust.origin_branches_holding_head(self.git, self.root))

    def test_a_branch_following_origin_at_a_ref_that_is_not_a_branch_is_refused(self) -> None:
        for merge in ("refs/merge-requests/7/head", "refs/remotes/origin/pr/7", "refs/tags/v1"):
            with self.subTest(merge=merge):
                follow(self.root, self.git, "origin", merge)
                self.assertEqual(f"its branch main follows {merge}, not a branch", self.foreign())

    def test_a_setting_or_head_git_cannot_read_raises_never_read_as_unset(self) -> None:
        refused = GitResult(1, "", "error: could not lock config file .git/config\n")
        for word in ("branch.main.remote", "branch.main.merge", "symbolic-ref"):

            def run(command: Sequence[str], timeout: float, word: str = word) -> GitResult:
                return refused if word in command else subprocess_runner(command, timeout)

            with self.subTest(word=word), self.assertRaises(GitError) as raised:
                checkout_trust.foreign_checkout(GitClient(run), self.root)
            self.assertIn("could not lock config file", str(raised.exception))

    def test_origin_s_refspecs_git_cannot_read_raise_never_read_as_unset(self) -> None:
        self.git.output(["checkout", "-q", "--detach", "refs/remotes/origin/main"], directory=self.root)

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if "remote.origin.fetch" in command:
                return GitResult(1, "", "error: could not lock config file .git/config\n")
            return subprocess_runner(command, timeout)

        with self.assertRaises(GitError) as raised:
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


def module_of(name: str) -> str:
    """The dotted module a file stands for, by its path from the repository root, such as tools.runtime_canary."""
    return name.removesuffix(".py").replace("/", ".")


def is_canary_option(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == CANARY_OPTION


class DeployerReader:
    """Where a module reaches a canary home, and whether a checkout_trust.refusal call comes before each place.

    Names resolve in their lexical scopes through the module's imports and simple assignments, as validation's
    CallableResolver reads them, and the runtime canary's own functions stand for themselves inside it."""

    def __init__(self, tree: ast.Module, module: str) -> None:
        self.tree = tree
        self.module = module
        self.resolver = CallableResolver(tree)
        self.functions: dict[int, Function] = {}
        self._enclose(tree, tree)
        self.refusals = [
            node for node in ast.walk(tree) if isinstance(node, ast.Call) and self.name(node.func) in REFUSALS
        ]

    def _enclose(self, node: ast.AST, function: Function) -> None:
        """Record the innermost function or lambda each node runs in, or the module itself."""
        for child in ast.iter_child_nodes(node):
            self.functions[id(child)] = function
            self._enclose(child, child if isinstance(child, (*DEFINED_FUNCTIONS, ast.Lambda)) else function)

    def name(self, node: ast.AST) -> str:
        """The dotted name an expression stands for, with a function the runtime canary defines at its top level
        standing for itself there."""
        name = self.resolver.name(node)
        local = name.removeprefix("<local>.")
        if local != name and self.module in CANARY_MODULES and self.binds_at_top(node, local):
            return f"{self.module}.{local}"
        return name

    def binds_at_top(self, node: ast.AST, name: str) -> bool:
        return (
            isinstance(node, ast.Name)
            and self.resolver.scopes[id(node)].owner(name) is self.resolver.scopes[id(self.tree)]
        )

    def names_a_canary_function(self, node: ast.AST) -> bool:
        """Whether an expression reads, never binds, the runtime canary's function that makes a home or deploys."""
        return (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(node.ctx, ast.Load)
            and self.name(node) in CANARY_NAMES
        )

    def reaches(self) -> bool:
        """Whether the module passes --canary-home or uses the runtime canary's function that makes a home or
        deploys."""
        return any(is_canary_option(node) or self.names_a_canary_function(node) for node in ast.walk(self.tree))

    def uses(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Name]:
        """Each place the module reads the name a function's definition binds, in the scope that binds it."""
        scopes = self.resolver.scopes
        owner = scopes[id(function)].owner(function.name)
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name)
            and node.id == function.name
            and isinstance(node.ctx, ast.Load)
            and scopes[id(node)].owner(node.id) is owner
        ]

    def guarded(self, node: ast.AST, followed: frozenset[int] = frozenset()) -> bool:
        """Whether a refusal comes before a place in its own function or, failing that, before every place the module
        names that function; a place at the top of the module, in a lambda, or in a function never named is not."""
        function = self.functions[id(node)]
        if any(self.functions[id(call)] is function and position(call) < position(node) for call in self.refusals):
            return True
        if not isinstance(function, DEFINED_FUNCTIONS) or id(function) in followed:
            return False
        uses = self.uses(function)
        return bool(uses) and all(self.guarded(use, followed | {id(function)}) for use in uses)

    def unguarded_lines(self) -> list[int]:
        """The lines of each call of the runtime canary's function that makes a home or deploys, and each
        --canary-home option, that no refusal comes before."""
        places = [
            node
            for node in ast.walk(self.tree)
            if is_canary_option(node) or (isinstance(node, ast.Call) and self.names_a_canary_function(node.func))
        ]
        return sorted({getattr(node, "lineno", 0) for node in places if not self.guarded(node)})


def position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def unguarded_deployers(sources: dict[str, str]) -> list[str]:
    """The modules, by their paths, that reach a canary home and never call checkout_trust.refusal, or reach one at a
    place no refusal comes before."""
    unguarded: list[str] = []
    for name, text in sorted(sources.items()):
        reader = DeployerReader(ast.parse(text), module_of(name))
        if reader.reaches() and (not reader.refusals or reader.unguarded_lines()):
            unguarded.append(name)
    return unguarded


class EveryDeployerRefusesTests(unittest.TestCase):
    """Every tool that deploys into a canary home refuses a foreign checkout first, through the one shared refusal.

    The reader resolves each name in its lexical scope through the module's imports and simple assignments, as
    validation's CallableResolver reads them, and follows a function the module defines through each place it names the
    function in the scope that binds it. It does not follow:
    - control flow: a refusal counts before a deployment written below it, on whichever branch, and whatever the
      module does with the refusal's result;
    - a canary function passed as a value, such as tools/skill_evals.py's Seams default, and called through another
      name: the module only has to call the refusal somewhere;
    - a method, or a function the module calls through an attribute, which has no place the module names it, so a
      deployment there is read as unguarded, as one in a function that calls itself is;
    - the --canary-home option where it is used rather than written: in a constant at the top of a module it is read
      at the top, where no function's refusal comes before it;
    - an import through importlib or __import__, a star import, or a name bound more than once in its scope, which
      CallableResolver leaves unresolved rather than guess which binding a read sees.
    """

    def test_every_tool_reaching_a_canary_home_calls_the_shared_refusal_first(self) -> None:
        sources = {
            path.relative_to(REPOSITORY).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY / "tools").glob("*.py"))
        }
        self.assertEqual([], unguarded_deployers(sources))
        reaching = {
            name for name, text in sources.items() if DeployerReader(ast.parse(text), module_of(name)).reaches()
        }
        self.assertTrue(
            {"tools/runtime_canary.py", "tools/plugin_canary.py", "tools/skill_evals.py"} <= reaching, reaching
        )

    def test_a_module_reaching_a_canary_home_without_the_refusal_is_named(self) -> None:
        violating = {
            "option.py": 'ARGUMENTS = ["--canary-home", "home"]\n',
            "imported.py": "from tools.runtime_canary import create_home\n\nhome = create_home()\n",
            "renamed.py": "from tools.runtime_canary import deploy as put\n\nput(home, source)\n",
            "attribute.py": "from tools import runtime_canary\n\nruntime_canary.deploy(home, source)\n",
            "aliased.py": "from tools import runtime_canary as rc\n\nrc.deploy(home, source)\n",
            "qualified.py": "import tools.runtime_canary\n\ntools.runtime_canary.deploy(home, source)\n",
            "bare.py": "import runtime_canary as canary\n\ncanary.start_home()\n",
        }
        self.assertEqual(sorted(violating), unguarded_deployers(violating))

    def test_a_module_reaching_a_canary_home_before_its_refusal_is_named(self) -> None:
        head = "from tools import checkout_trust\nfrom tools import runtime_canary as rc\n\n\n"
        refuse = "    if checkout_trust.refusal(checkout_trust.reader(root)) is not None:\n        return 1\n"
        violating = {
            "late.py": f"def main():\n    rc.deploy(home, source)\n{refuse}",
            "helper.py": f"def run():\n    rc.deploy(home, source)\n\n\ndef main():\n    run()\n{refuse}    run()\n",
            "other_function.py": f"def main():\n{refuse}    return 0\n\n\ndef other():\n    rc.deploy(home, source)\n",
            "top.py": f"def main():\n{refuse}    return 0\n\n\nrc.deploy(home, source)\n",
            "unnamed.py": f"def main():\n{refuse}    return 0\n\n\ndef entry():\n    rc.create_home()\n",
            "option_first.py": f"def main():\n    arguments = ['--canary-home', home]\n{refuse}",
            "elsewhere.py": f"def check():\n{refuse}\n\ndef main():\n    rc.deploy(home, source)\n    check()\n",
            "assigned.py": f"put = rc.deploy\n\n\ndef main():\n    put(home, source)\n{refuse}",
        }
        violating = {name: head + body for name, body in violating.items()}
        self.assertEqual(sorted(violating), unguarded_deployers(violating))

    def test_a_module_that_refuses_first_or_reaches_no_home_is_not_named(self) -> None:
        conforming = {
            "guarded.py": (
                "from tools import checkout_trust, runtime_canary\n\n"
                "if checkout_trust.refusal(checkout_trust.reader(root)) is None:\n"
                "    runtime_canary.deploy(home, source)\n"
            ),
            "through_helpers.py": (
                "import tools.checkout_trust as trust\n"
                "from tools import runtime_canary as rc\n\n\n"
                "def deploy_home(home):\n"
                "    def put():\n"
                "        return rc.deploy(home, source)\n\n"
                "    return put()\n\n\n"
                "def run(home):\n"
                "    return deploy_home(home), ['--canary-home', str(home)]\n\n\n"
                "def main():\n"
                "    if trust.refusal(trust.reader(root)) is not None:\n"
                "        return 1\n"
                "    home = rc.start_home(rc.create_home)\n"
                "    return run(home)\n"
            ),
            "passed_as_a_value.py": (
                "from tools import checkout_trust, runtime_canary\n\n\n"
                "class Seams:\n"
                "    deploy = runtime_canary.deploy\n\n\n"
                "def main(seams):\n"
                "    if checkout_trust.refusal(seams.checkout) is None:\n"
                "        seams.deploy(home, source)\n"
            ),
            "unrelated.py": "from tools.runtime_canary import quote\n\ndeploy = quote\n\nprint('--canary-homes')\n",
            "own_deploy.py": "def deploy(home):\n    return home\n\n\ndeploy(home)\n",
            # Another function's local variable of the same name is no place the module names the function.
            "shadowed.py": (
                "from tools import checkout_trust, runtime_canary\n\n\n"
                "def run():\n"
                "    runtime_canary.deploy(home, source)\n\n\n"
                "def listed():\n"
                "    run = []\n"
                "    return run\n\n\n"
                "def main():\n"
                "    if checkout_trust.refusal(checkout_trust.reader(root)) is None:\n"
                "        run()\n"
            ),
        }
        self.assertEqual([], unguarded_deployers(conforming))

"""The runtime catalogue and the reader of a skill's runtime_support declaration (runtime_support.py)."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import python_reader
import runtime_support
from git_client import GitClient
from powershell_commands import UNREAD_WORD

PARTIAL = {"level": "partial", "needs": ["sandbox-network"], "reason": "Its gh calls need the network."}


def test_a_declaration_reads_full_and_partial_runtimes() -> None:
    support = runtime_support.parse({"claude-code": "full", "codex": PARTIAL, "copilot-cli": "full"})
    assert support["claude-code"] == runtime_support.Support("full")
    assert support["codex"] == runtime_support.Support(
        "partial", ("sandbox-network",), "Its gh calls need the network."
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object keyed by runtime"),
        ({"claude-code": "full", "codex": "full"}, "does not declare copilot-cli"),
        ({"claude-code": "full", "codex": "full", "copilot-cli": "full", "other": "full"}, "unknown runtime 'other'"),
        ({"claude-code": "full", "codex": {**PARTIAL, "needs": []}, "copilot-cli": "full"}, "names no need"),
        ({"claude-code": "full", "codex": {**PARTIAL, "needs": ["flight"]}, "copilot-cli": "full"}, "'flight'"),
        ({"claude-code": "full", "codex": {**PARTIAL, "reason": "a\nb"}, "copilot-cli": "full"}, "one-line reason"),
    ],
)
def test_a_declaration_out_of_shape_names_its_problem(value: object, message: str) -> None:
    with pytest.raises(runtime_support.RuntimeSupportError, match=message):
        runtime_support.parse(value)


def test_the_needs_follow_from_grants_start_hooks_tools_and_git() -> None:
    assert runtime_support.frontmatter_needs(["Agent", "Read"], False) == {"agent-delegation"}
    assert runtime_support.frontmatter_needs(["Read"], False, starts_hooked_agent=True) == set()
    assert runtime_support.frontmatter_needs(["Agent"], True, starts_hooked_agent=True) == {
        "agent-delegation",
        "fail-closed-agent-hooks",
        "user-only-start",
    }
    assert runtime_support.frontmatter_needs([], False, tools=frozenset({"pdftotext", "gh"})) == {"sandbox-network"}
    assert runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({"status"})) == set()
    assert runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({"fetch"})) == {"sandbox-network"}


# A subcommand no literal names, recorded as the word that stands for it, may be any subcommand, one that reaches a
# remote included; a subcommand a literal names reaches one only when it is one of NETWORK_GIT_SUBCOMMANDS.
@pytest.mark.parametrize(
    ("subcommand", "reaches"),
    [
        ("push", True),
        ("ls-remote", True),
        ("status", False),
        ("rev-parse", False),
        ("$sub", True),
        ("${subcommand}", True),
        ("<value>", True),
        ("Push", True),
        ("", True),
    ],
)
def test_a_subcommand_no_literal_names_may_reach_a_remote(subcommand: str, reaches: bool) -> None:
    assert runtime_support.reaches_remote(subcommand) is reaches
    needs = runtime_support.frontmatter_needs([], False, git_subcommands=frozenset({subcommand}))
    assert needs == ({"sandbox-network"} if reaches else set())


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('services.git.run(["fetch", "origin", "--quiet"], directory=root)', {"fetch"}),
        ('git_in(checkout, runner, "ls-remote", "--symref", "origin", "HEAD")', {"ls-remote"}),
        ('git(services, root, "-C", path, "push", "origin")', {"push"}),
        ('GitClient(runner).output(["-c", "core.quotepath=off", "clone", url])', {"clone"}),
        ('self.client.run(("pull",))', set()),
        # A value that stands where git reads its subcommand may be any subcommand, so it is recorded as the word no
        # literal names, which reaches_remote reads as one that may reach a remote. A starred value carries git's
        # words wherever it stands.
        ('services.git.run(["--no-optional-locks", *arguments])', {UNREAD_WORD}),
        ('git.output(["-C", directory, verb])', {UNREAD_WORD}),
        ('git.output([verb, "origin"])', {UNREAD_WORD}),
        ('git.output((*options, "fetch"))', {UNREAD_WORD}),
        ("git.output([verb])", {UNREAD_WORD}),
        ('git(services, root, "-C", path, verb)', {UNREAD_WORD}),
        ("git_output(services, root, *arguments)", {UNREAD_WORD}),
        ("git_in(checkout, git, *fetch)", {UNREAD_WORD}),
        ('git(services, root, *SHOW_UNTRACKED, "worktree", "remove", path)', {UNREAD_WORD}),
        ("services.git.commit_of(ref, directory=root)", set()),
        ("services.git.run(arguments, directory=root)", set()),
        ('READ.json_object(value, "pull")', set()),
        ('digits(value, "fetch")', set()),
        ('print("fetch")', set()),
    ],
)
def test_a_python_git_call_names_its_subcommand(line: str, expected: set[str]) -> None:
    # The derivation reads a Python script's git calls as it reads a Bash script's, so a Python-only skill that
    # fetches needs sandbox-network too.
    assert runtime_support.python_git_subcommands(line) == expected


def test_a_python_script_that_does_not_parse_raises() -> None:
    with pytest.raises(python_reader.PythonRefused):
        runtime_support.python_git_subcommands("git(")


def test_an_invoked_skill_is_the_first_word_of_a_code_span_on_a_line_that_says_invoke() -> None:
    text = (
        "Then invoke `review-prs --re-review`, or invoke `flag-review-finding`.\n"
        "Run `repo-cleanup` once.\n"
        "Invoke `review-document` here.\n"
        "invoke `unknown-skill` and `review-insights`\n"
        "invoke ` ` and ``\n"
    )
    names = {"review-prs", "flag-review-finding", "repo-cleanup", "review-document", "review-insights"}
    assert runtime_support.invoked_skills(text, names) == {"review-prs", "flag-review-finding", "review-insights"}


@pytest.mark.parametrize("separator", ["\x0c", "\x85", "\u2028", "\u2029"])
def test_a_line_ends_where_commonmark_ends_one_so_a_separator_keeps_the_span_on_the_invoke_line(separator: str) -> None:
    assert runtime_support.invoked_skills(f"Then invoke{separator}`review-prs`\n", {"review-prs"}) == {"review-prs"}
    assert runtime_support.invoked_skills("Then invoke\r`review-prs`\n", {"review-prs"}) == set()


def test_an_invoked_skill_carries_its_needs_and_its_own_invoked_skills_but_not_user_only_start() -> None:
    own = {
        "dashboard": {"sandbox-network"},
        "review": {"agent-delegation", "user-only-start"},
        "flag": {"workflow"},
        "loop": set(),
    }
    invokes = {"dashboard": {"review", "dashboard"}, "review": {"flag", "dashboard"}, "flag": {"loop"}, "loop": set()}
    carried = runtime_support.carried_needs("dashboard", own.__getitem__, invokes.__getitem__)
    assert carried == {"review": {"agent-delegation", "workflow"}}
    assert list(runtime_support.carried_needs("review", own.__getitem__, invokes.__getitem__)) == ["dashboard", "flag"]
    assert runtime_support.carried_needs("loop", own.__getitem__, invokes.__getitem__) == {}


def test_what_a_runtime_lacks_decides_its_declaration() -> None:
    needs = {"workflow", "sandbox-network"}
    assert runtime_support.lacking("claude-code", needs) == ()
    assert runtime_support.lacking("codex", needs) == ("workflow", "sandbox-network")
    assert runtime_support.declaration(needs)["copilot-cli"] == {
        "level": "partial",
        "needs": ["workflow"],
        "reason": runtime_support.DEFAULT_REASONS["workflow"],
    }


def test_a_codex_reason_for_lacking_the_network_points_to_the_codex_guide() -> None:
    pointer = 'See "Network access" in docs/codex-support.md for the ways to open the network.'
    assert runtime_support.guide_pointer("codex", "sandbox-network") == pointer
    assert runtime_support.guide_pointer("copilot-cli", "sandbox-network") is None
    assert runtime_support.guide_pointer("codex", "workflow") is None
    assert runtime_support.declaration({"workflow", "sandbox-network"})["codex"] == {
        "level": "partial",
        "needs": ["workflow", "sandbox-network"],
        "reason": " ".join(
            [runtime_support.DEFAULT_REASONS["workflow"], runtime_support.DEFAULT_REASONS["sandbox-network"], pointer]
        ),
    }


@pytest.mark.parametrize(
    ("call", "named"),
    [
        ("git(x)", True),
        ("services.git.run(x)", True),
        ("git_in(x)", True),
        ("GitClient(runner).output(x)", True),
        ("run_git(x)", True),
        ("GitHubClient(runner).output(x)", False),
        ("github_client.json(x)", False),
        ("self.github.run(x)", False),
        ("digit(x)", False),
        ("legit(x)", False),
        # A method of what a call gives back is that value's: only a GitClient constructed in place is a client.
        ("git_output(x).split(y)", False),
        ("git.output(x).split(y)", False),
        ("_read_git_file(x).decode(y)", False),
        ("make(git).output(y)", False),
    ],
)
def test_a_call_names_git_by_a_word_of_its_name_github_not_counting(call: str, named: bool) -> None:
    expression = ast.parse(call, mode="eval").body
    assert isinstance(expression, ast.Call)
    assert runtime_support.names_git(expression.func) is named


def script_subcommands(source: str) -> set[str]:
    return runtime_support.python_git_subcommands(textwrap.dedent(source))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A separator given to a method of a git call's output is no subcommand.
        ('git_output(services, root, "ls-files", "-v", "-z").split("\\x00")', {"ls-files"}),
        ('git_client.output_line(git_output(services, root, "rev-list", "--count")).split("\\t")', {"rev-list"}),
        ('git.output(["ls-remote", "--heads", "origin"], directory=top).split("\\n")', {"ls-remote"}),
        ('_read_git_file(checkout, commit, skill, runner).decode("utf-8-sig")', set()),
        # A GitClient method that names its own subcommand takes no word of git's.
        ('git.commit_of("HEAD", directory=checkout)', set()),
        ('services.git.rev_parse_path("--show-toplevel", directory=cwd)', set()),
        ('GitClient(runner).file_at("HEAD", "a b")', set()),
        # git_client's own callables start no git, however a script imports them.
        ('from git_client import GitError\nraise GitError("git could not read the HEAD commit")', set()),
        ('from git_client import GitError as Failed\nraise Failed("git fetch failed")', set()),
        ('import git_client\nraise git_client.GitError("git fetch failed")', set()),
        ('import git_client as gc\nraise gc.GitError("git fetch failed")', set()),
        ('import git_client\ngit_client.isolated_git_environment(["fetch"])', set()),
    ],
)
def test_only_a_call_that_starts_git_names_a_subcommand(source: str, expected: set[str]) -> None:
    assert script_subcommands(source) == expected


def test_a_helper_of_the_script_named_for_git_that_starts_none_names_no_subcommand() -> None:
    source = """
        class GitUnavailable(Exception):
            pass

        def git_failure(root, command, exc):
            if not exc.answered:
                return GitUnavailable(f"could not run git: {exc}")
            return CleanupError(f"{command} failed in {root}")

        def git_blob_id(algorithm, size, chunks):
            return hashlib.new(algorithm, b"".join(chunks)).hexdigest()

        raise git_failure(root, "git rev-parse", exc)
        git_blob_id("sha1", 3, [*chunks])
    """
    assert script_subcommands(source) == set()


def test_helpers_named_for_git_that_only_call_each_other_name_no_subcommand() -> None:
    # A reader's recursion, as git_subcommands' command_git_subcommands and bash_git_subcommands make, starts no git.
    source = """
        def command_git_subcommands(words):
            return bash_git_subcommands(" ".join(words)) if words else set()

        def bash_git_subcommands(text):
            return command_git_subcommands(["bash", "-c", *text.split()])
    """
    assert script_subcommands(source) == set()


@pytest.mark.parametrize(
    "wrapper",
    [
        # Through a git client's method, through another wrapper, through a client whose name says nothing of git,
        # and through a module's wrapper.
        "def git(services, root, *arguments):\n    return services.git.run(arguments, directory=root)",
        "def git(services, root, *arguments):\n    return run_git(services, root, *arguments)\n"
        "def run_git(services, root, *arguments):\n    return GitClient(services.runner).output(arguments)",
        "class Store:\n    def git(self, *arguments):\n        return self.client.output(arguments, directory=here)",
        "import worktrees\ndef git(cwd, *arguments):\n    return worktrees.git(cwd, *arguments)",
    ],
)
def test_a_wrapper_of_the_script_that_starts_git_keeps_its_callers_subcommands(wrapper: str) -> None:
    # The wrapper's own call passes its words on, starred, so it names the word no literal does; its callers name
    # theirs.
    source = f"{wrapper}\ngit(services, root, 'fetch', 'origin')\nstore.git('push', 'origin')\n"
    assert script_subcommands(source) - {UNREAD_WORD} == {"fetch", "push"}


def test_a_script_s_own_quiet_helper_never_stands_for_a_modules_git() -> None:
    source = """
        import worktrees

        def git(message):
            return message

        worktrees.git(hub, "fetch", "origin")
        git("push")
    """
    assert script_subcommands(source) == {"fetch"}


def test_the_methods_that_take_gits_words_are_the_clients_that_take_arguments_first() -> None:
    # Read from the client itself, so a method added there that takes git's words is read too.
    taking = {
        name
        for name, member in GitClient.__dict__.items()
        if inspect.isfunction(member)
        and not name.startswith("_")
        and list(inspect.signature(member).parameters)[1:2] == ["arguments"]
    }
    assert taking == runtime_support.GIT_ARGUMENT_METHODS

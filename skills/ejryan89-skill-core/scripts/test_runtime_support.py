"""The runtime catalogue and the reader of a skill's runtime_support declaration (runtime_support.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_support

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


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('services.git.run(["fetch", "origin", "--quiet"], directory=root)', {"fetch"}),
        ('git_in(checkout, runner, "ls-remote", "--symref", "origin", "HEAD")', {"ls-remote"}),
        ('git(services, root, "-C", path, "push", "origin")', {"push"}),
        ('GitClient(runner).output(["-c", "core.quotepath=off", "clone", url])', {"clone"}),
        ('self.client.run(("pull",))', set()),
        ("git_output(services, root, *arguments)", set()),
        ('services.git.run(["--no-optional-locks", *arguments])', set()),
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
    with pytest.raises(SyntaxError):
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

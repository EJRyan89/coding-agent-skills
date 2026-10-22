"""Regression suite for tools/plugin_canary.py: installing the plugins into a throwaway configuration and judging what
Claude Code listed and ran, with a fake runner in place of Claude Code."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

from review_dispatch import REVIEWER_TASK

from tools import plugin_build
from tools import plugin_canary as canary
from tools.runtime_canary import FIXTURE_SKILL, FIXTURE_SOURCE, REPOSITORY_ROOT, RUN_ID, STATE, Completed

MARKER = "PLUGIN-CANARY-OUTSIDE-1a2b"
AGENT = "coding-agent-skills:code-review-reviewer"


def event(kind: str, content: list[dict[str, Any]], parent: str | None = None) -> str:
    return json.dumps({"type": kind, "parent_tool_use_id": parent, "message": {"content": content}})


def result(text: object, *, error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "content": text, "is_error": error}


DENIED = "PreToolUse:Read hook error: Code-review reviewer boundary: read the prompt file your task names first; x"


def test_the_main_session_reading_and_the_reviewer_denied_is_a_pass() -> None:
    stream = "\n".join(
        [
            "not json",
            event("user", [result(f"1\t{MARKER}\n")]),
            event("user", [result(DENIED, error=True)], parent="toolu_1"),
            event("user", [result([{"type": "text", "text": "the reviewer reports"}])]),
        ]
    )
    lines = canary.guard_lines(stream, AGENT, MARKER)
    assert lines == [
        "GUARD claude main-session PASSED",
        f'GUARD claude {AGENT} DENIED "Code-review reviewer boundary: read the prompt file your task names first"',
    ]
    assert not any(canary.failed(line) for line in lines)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (
            [event("user", [result(f"1\t{MARKER}\n")]), event("user", [result(f"1\t{MARKER}")], parent="t")],
            f"GUARD claude {AGENT} PASSED",
        ),
        ([event("user", [result(f"1\t{MARKER}\n")])], f'GUARD claude {AGENT} FAILED "the reviewer made no call'),
        (
            [event("user", [result(f"1\t{MARKER}\n")]), event("user", [result("Permission denied", error=True)], "t")],
            f'GUARD claude {AGENT} FAILED "the reviewer made no call',
        ),
    ],
)
def test_a_reviewer_that_read_the_file_or_met_no_guard_fails(stream: list[str], expected: str) -> None:
    lines = canary.guard_lines("\n".join(stream), AGENT, MARKER)
    assert lines[1].startswith(expected)
    assert canary.failed(lines[1])


def test_a_main_session_the_guard_denied_or_that_never_read_fails() -> None:
    denied = canary.guard_lines(event("user", [result(DENIED, error=True)]), AGENT, MARKER)[0]
    assert denied.startswith('GUARD claude main-session DENIED "Code-review reviewer boundary')
    unread = canary.guard_lines(event("user", [result("other")]), AGENT, MARKER)[0]
    assert unread == 'GUARD claude main-session FAILED "the session never read the marker file"'
    assert canary.failed(denied)
    assert canary.failed(unread)


def test_the_component_inventory_is_read_from_plugin_details() -> None:
    text = (
        "coding-agent-skills 0.0.0-dev\n\nComponent inventory\n"
        "  Skills (2)  alpha, beta-two\n  Agents (1)  code-review-reviewer\n"
        "  Hooks (1)  PreToolUse  (harness-only — no model context cost)\n  MCP servers (0)\n  Agents (0)\n"
    )
    assert canary.details(text) == {
        "skill": {"alpha", "beta-two"},
        "agent": {"code-review-reviewer"},
        "hook": {"PreToolUse"},
    }


@pytest.mark.parametrize(
    ("line", "fails"),
    [
        ("VALIDATED claude a", False),
        ('VALIDATION_FAILED claude a "x"', True),
        ('INSTALL_FAILED claude a "x"', True),
        ("UNDISCOVERED claude a:b skill", True),
        ('RUNTIME claude a:b RAN "s" "c"', False),
        ('RUNTIME claude a:b FAILED "x"', True),
        ("INSTALLED claude a 1.0.0", False),
        ('UNGUARDED claude a:b REFUSED "x"', False),
        ("UNGUARDED claude a:b ACCEPTED", True),
        ('UNGUARDED claude a:b FAILED "x"', True),
    ],
)
def test_each_kind_of_line_passes_or_fails_the_run(line: str, fails: bool) -> None:
    assert canary.failed(line) is fails


class FakeClaude:
    """Answers each plugin command as Claude Code does, recording the environment each one ran with."""

    def __init__(self, home: Path, listed: dict[str, set[str]] | None = None) -> None:
        self.home = home
        self.listed = listed
        self.plugins: list[plugin_build.Plugin] = []
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        self.calls.append((arguments, environment))
        command = arguments[1:3]
        if command == ["plugin", "list"]:
            installed = [
                {"id": f"{name}@canary", "version": "0.0.0-dev", "installPath": str(self.home / "cache" / name)}
                for name in ("coding-agent-skills", "coding-agent-skills-dotnet", "runtime-canary")
            ]
            return Completed(0, json.dumps(installed), "")
        if command == ["plugin", "details"]:
            name = arguments[3].split("@")[0]
            plugin = next(p for p in self.plugins if p.name == name)
            listed = self.listed if self.listed is not None else canary.expected(plugin)
            text = "\n".join(f"  {kind.title()}s (1)  {', '.join(sorted(names))}" for kind, names in listed.items())
            return Completed(0, text, "")
        return Completed(0, "ok", "")


def session(home: Path, runner: FakeClaude) -> canary.Session:
    return canary.Session(home, runner, {"PATH": "x", "CLAUDE_CONFIG_DIR": "the user's"}, 30)


def test_the_plugins_are_built_validated_installed_and_listed_in_the_throwaway_configuration(tmp_path: Path) -> None:
    plugins = canary.build(tmp_path, [REPOSITORY_ROOT, FIXTURE_SOURCE])
    assert [plugin.name for plugin in plugins] == [
        "coding-agent-skills",
        "coding-agent-skills-dotnet",
        "runtime-canary",
    ]
    marketplace = json.loads((tmp_path / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert [entry["source"] for entry in marketplace["plugins"]] == [f"./{plugin.name}" for plugin in plugins]
    assert (tmp_path / "coding-agent-skills" / "hooks" / "hooks.json").is_file()
    runner = FakeClaude(tmp_path)
    runner.plugins = plugins
    lines = list(canary.install(session(tmp_path, runner), "claude", plugins))
    assert lines[:3] == [f"VALIDATED claude {plugin.name}" for plugin in plugins]
    assert "INSTALLED claude coding-agent-skills 0.0.0-dev" in lines
    assert "DISCOVERED claude coding-agent-skills:code-review-reviewer agent" in lines
    assert "DISCOVERED claude coding-agent-skills:PreToolUse hook" in lines
    assert "DISCOVERED claude runtime-canary:runtime-canary-probe skill" in lines
    assert not any(canary.failed(line) for line in lines)
    paths = json.loads(lines[-1].removeprefix("PATHS "))
    assert paths["runtime-canary"] == str(tmp_path / "cache" / "runtime-canary")
    # A plugin with a required option is installed with a value for it: a folder of the throwaway home.
    installs = [arguments[3:] for arguments, _ in runner.calls if arguments[1:3] == ["plugin", "install"]]
    root = tmp_path / "options" / "repos_root"
    assert installs == [
        ["coding-agent-skills@canary", "--config", f"repos_root={root}"],
        ["coding-agent-skills-dotnet@canary"],
        ["runtime-canary@canary"],
    ]
    assert root.is_dir()
    # Every command reads and writes the throwaway configuration, never the user's.
    assert {environment["CLAUDE_CONFIG_DIR"] for _, environment in runner.calls} == {str(tmp_path / canary.CONFIG)}


def test_a_component_the_installed_plugin_does_not_list_is_undiscovered(tmp_path: Path) -> None:
    plugins = canary.build(tmp_path, [FIXTURE_SOURCE])
    runner = FakeClaude(tmp_path, listed={"skill": set()})
    runner.plugins = plugins
    lines = list(canary.install(session(tmp_path, runner), "claude", plugins))
    assert "UNDISCOVERED claude runtime-canary:runtime-canary-probe skill" in lines


def test_a_plugin_that_fails_validation_or_install_is_reported(tmp_path: Path) -> None:
    plugins = canary.build(tmp_path, [FIXTURE_SOURCE])

    def refusing(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        return Completed(1, "", "✘ Validation failed\n")

    lines = list(canary.install(canary.Session(tmp_path, refusing, {}, 30), "claude", plugins))
    assert lines == [
        'VALIDATION_FAILED claude runtime-canary "✘ Validation failed"',
        'INSTALL_FAILED claude runtime-canary "✘ Validation failed"',
        "PATHS {}",
    ]


def test_the_probe_runs_the_installed_copy_and_records_beside_it(tmp_path: Path) -> None:
    installed = tmp_path / "cache" / "runtime-canary" / "0.0.0-dev"
    script = installed / "skills" / FIXTURE_SKILL / "scripts" / "canary_probe.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    (tmp_path / STATE).mkdir()

    def probing(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        assert ["--plugin-dir", str(installed)] == arguments[arguments.index("--plugin-dir") :][:2]
        assert arguments[2].startswith(f"/runtime-canary:{FIXTURE_SKILL} ")
        state = installed.parent / STATE
        run = (state / RUN_ID).read_text(encoding="utf-8")
        record = {"script": script.as_posix(), "cwd": cwd.as_posix(), "run": run}
        (state / "probe.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        return Completed(0, "", "")

    lines = canary.probe(
        canary.Session(tmp_path, probing, {}, 30), "claude", {"runtime-canary": str(installed)}, "runtime-canary"
    )
    assert lines[0].startswith(f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} RAN "{script.as_posix()}"')
    assert lines[1].startswith("TRANSCRIPT claude probe ")


def test_a_probe_that_never_ran_the_installed_copy_fails(tmp_path: Path) -> None:
    installed = tmp_path / "cache" / "runtime-canary" / "0.0.0-dev"
    installed.mkdir(parents=True)
    (tmp_path / STATE).mkdir()

    def silent(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        return Completed(0, "", "")

    lines = canary.probe(
        canary.Session(tmp_path, silent, {}, 30), "claude", {"runtime-canary": str(installed)}, "runtime-canary"
    )
    assert lines[0].startswith(f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} FAILED "the installed copy')
    assert canary.failed(lines[0])


def test_a_probe_session_that_discovered_no_skill_fails_as_a_bare_session_would(tmp_path: Path) -> None:
    installed = tmp_path / "cache" / "runtime-canary" / "0.0.0-dev"
    installed.mkdir(parents=True)
    (tmp_path / STATE).mkdir()

    def bare(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        return Completed(0, json.dumps({"type": "system", "subtype": "init", "skills": []}) + "\n", "")

    lines = canary.probe(
        canary.Session(tmp_path, bare, {}, 30), "claude", {"runtime-canary": str(installed)}, "runtime-canary"
    )
    assert lines[0] == (
        f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} FAILED "Claude Code discovered no skill, as a session in bare '
        'mode does"'
    )
    assert canary.failed(lines[0])


def test_the_guard_session_gives_the_reviewer_a_reviewers_task_naming_the_marker(tmp_path: Path) -> None:
    (tmp_path / STATE).mkdir()
    seen: list[list[str]] = []

    def guarded(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        seen.append(arguments)
        assert "CLAUDE_CONFIG_DIR" not in environment
        marker = (tmp_path / "outside.txt").read_text(encoding="utf-8").strip()
        return Completed(
            0, "\n".join([event("user", [result(marker)]), event("user", [result(DENIED, error=True)], "t")]), ""
        )

    lines = canary.guard(
        canary.Session(tmp_path, guarded, {"PATH": "x"}, 30), "claude", {"a": "b"}, "coding-agent-skills"
    )
    task = REVIEWER_TASK.format(prompt=tmp_path / "outside.txt")
    assert f"subagent_type {AGENT}, with exactly this prompt: {task}" in seen[0][2]
    assert lines[:2] == [
        "GUARD claude main-session PASSED",
        f'GUARD claude {AGENT} DENIED "Code-review reviewer boundary: read the prompt file your task names first"',
    ]


def unguarded_session(home: Path, writes: bool, claims: bool, seen: list[list[str]]) -> canary.Session:
    """A session whose reviewer writes its role's result, or not, and whose guard claims the role, or not."""

    def reviewing(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        seen.append(arguments)
        run = home / canary.UNGUARDED_RUN
        if claims:
            (run / "work").mkdir()
            (run / "work" / f"reads-{canary.UNGUARDED_ROLE}.log").touch()
        if writes:
            (run / f"{canary.UNGUARDED_ROLE}.json").write_text('{"canary": true}', encoding="utf-8")
        return Completed(0, "the reviewer reports", "")

    (home / STATE).mkdir(parents=True)
    return canary.Session(home, reviewing, {"PATH": "x"}, 30)


def test_a_reviewer_with_every_hook_off_writes_a_result_check_refuses(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    lines = canary.unguarded(
        unguarded_session(tmp_path, True, False, seen), "claude", {"a": "b"}, "coding-agent-skills"
    )
    command = seen[0]
    assert command[command.index("--settings") + 1] == '{"disableAllHooks": true}'
    assert command[command.index("--allowedTools") + 1 : command.index("--settings")] == ["Read", "Write", "Agent"]
    prompt = tmp_path / canary.UNGUARDED_RUN / f"{canary.UNGUARDED_ROLE}.prompt.md"
    task = f"Read {prompt} and follow it exactly. It is your complete task."
    assert f"subagent_type {AGENT}, with exactly this prompt: {task}" in command[2]
    roles = json.loads((tmp_path / canary.UNGUARDED_RUN / "run.json").read_text(encoding="utf-8"))["roles"]
    assert [role["prompt_file"] for role in roles] == [str(prompt)]
    assert lines[0].startswith(f'UNGUARDED claude {AGENT} REFUSED "the reviewer ran without its guard: ')
    assert lines[0].endswith('or prepare the review again with --inline"')
    assert not canary.failed(lines[0])


def test_a_result_the_guard_claimed_or_no_result_fails_the_check(tmp_path: Path) -> None:
    claimed = canary.unguarded(unguarded_session(tmp_path / "claimed", True, True, []), "claude", {}, "p")
    assert claimed[0] == "UNGUARDED claude p:code-review-reviewer ACCEPTED"
    silent = canary.unguarded(unguarded_session(tmp_path / "silent", False, False, []), "claude", {}, "p")
    assert (
        silent[0]
        == 'UNGUARDED claude p:code-review-reviewer FAILED "the reviewer wrote no result: the reviewer reports"'
    )
    assert canary.failed(claimed[0])
    assert canary.failed(silent[0])

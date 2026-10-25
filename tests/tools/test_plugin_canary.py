"""Regression suite for tools/plugin_canary.py: installing the plugins into a throwaway configuration and judging what
Claude Code listed and ran, with a fake runner in place of Claude Code."""

from __future__ import annotations

import json
import re
import shutil
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
from tools.runtime_canary import (
    FIXTURE_SKILL,
    FIXTURE_SOURCE,
    REPOSITORY_ROOT,
    RUN_ID,
    STATE,
    Completed,
    claim_home,
    create_home,
    quote,
)

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


def test_a_plugin_expects_the_hooks_its_hooks_file_names_and_none_without_one() -> None:
    hooks: dict[str, list[object]] = {"PreToolUse": [], "PostToolUse": []}
    cases: list[tuple[dict[str, bytes], set[str]]] = [
        ({"hooks/hooks.json": json.dumps({"hooks": hooks}).encode()}, set(hooks)),
        ({"hooks/hooks.json": b"{}"}, set()),
        ({}, set()),
    ]
    for files, found in cases:
        plugin = plugin_build.Plugin("suite", skills=["alpha"], agents=["reviewer"], files=files)
        assert canary.expected(plugin) == {"skill": {"alpha"}, "agent": {"reviewer"}, "hook": found}


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

    def __init__(
        self, home: Path, listed: dict[str, set[str]] | None = None, entries: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.home = home
        self.listed = listed
        # A plugin's `claude plugin list --json` entry in place of the one Claude Code writes.
        self.entries = entries or {}
        self.plugins: list[plugin_build.Plugin] = []
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        # Where each command ran and the limit it was given.
        self.places: list[tuple[Path, float]] = []

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        self.calls.append((arguments, environment))
        self.places.append((cwd, timeout))
        command = arguments[1:3]
        if command == ["plugin", "list"]:
            installed = [
                self.entries.get(
                    name,
                    {"id": f"{name}@canary", "version": "0.0.0-dev", "installPath": str(self.home / "cache" / name)},
                )
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


def test_every_plugin_command_the_canary_starts_is_one_its_builders_return(tmp_path: Path) -> None:
    # tools/runtime_command_lines.py starts these builders' command lines weekly, so the canary may start no other.
    plugins = canary.build(tmp_path, [REPOSITORY_ROOT, FIXTURE_SOURCE])
    runner = FakeClaude(tmp_path)
    runner.plugins = plugins
    list(canary.install(session(tmp_path, runner), "claude", plugins))
    expected = [canary.validate_command("claude", tmp_path / plugin.name) for plugin in plugins]
    expected.append(canary.marketplace_command("claude", tmp_path))
    for plugin in plugins:
        identifier = f"{plugin.name}@{canary.MARKETPLACE}"
        expected += [
            canary.install_command("claude", identifier, canary.option_values(tmp_path, plugin)),
            canary.list_command("claude"),
            canary.details_command("claude", identifier),
        ]
    assert [arguments for arguments, _ in runner.calls] == expected
    assert set(runner.places) == {(tmp_path, 30)}
    configured = next(plugin for plugin in plugins if plugin.name == "coding-agent-skills")
    assert canary.install_command("claude", "x@canary", canary.option_values(tmp_path, configured)) == [
        "claude",
        "plugin",
        "install",
        "x@canary",
        "--config",
        f"repos_root={tmp_path / canary.OPTIONS / 'repos_root'}",
    ]
    assert canary.validate_command("claude", tmp_path / "p") == [
        "claude",
        "plugin",
        "validate",
        "--strict",
        str(tmp_path / "p"),
    ]
    assert canary.marketplace_command("claude", tmp_path) == ["claude", "plugin", "marketplace", "add", str(tmp_path)]
    assert canary.list_command("claude") == ["claude", "plugin", "list", "--json"]
    assert canary.details_command("claude", "x@canary") == ["claude", "plugin", "details", "x@canary"]


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


@pytest.mark.parametrize(
    ("install_path", "reason"),
    [
        (None, "claude plugin list --json gives it no installPath"),
        ("", "claude plugin list --json gives it no installPath"),
        (7, "claude plugin list --json gives it no installPath"),
        ("cache/runtime-canary", "claude plugin list --json gives it a relative installPath cache/runtime-canary"),
        ("outside", "claude plugin list --json gives it an installPath outside the throwaway home {path}"),
        ("home", "claude plugin list --json gives it an installPath outside the throwaway home {path}"),
    ],
)
def test_an_installed_plugin_the_canary_cannot_place_in_the_home_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, install_path: object, reason: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "built").mkdir()
    places = {"outside": str(tmp_path / "elsewhere" / "runtime-canary"), "home": str(home)}
    path = places.get(install_path, install_path) if isinstance(install_path, str) else install_path
    entry: dict[str, Any] = {"id": "runtime-canary@canary", "version": "0.0.0-dev"}
    if path is not None:
        entry["installPath"] = path
    runner = FakeClaude(home, entries={"runtime-canary": entry})
    # The runner answers `claude plugin details` from plugins built apart; checks() builds its own into the home.
    runner.plugins = canary.build(tmp_path / "built", [REPOSITORY_ROOT, FIXTURE_SOURCE])
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    lines = list(canary.checks(canary.Session(home, runner, {}, 30), "claude", discovery_only=False))
    refusal = reason.format(path=path)
    assert f"INSTALL_FAILED claude runtime-canary {quote(refusal)}" in lines
    assert not any(line.startswith("INSTALLED claude runtime-canary") for line in lines)
    assert lines[-1] == (
        f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} FAILED "a plugin the sessions load is not installed"'
    )
    # Nothing is written outside the throwaway home: not in the working directory, and not beside the home.
    assert list(working.iterdir()) == []
    assert sorted(item.name for item in tmp_path.iterdir()) == ["built", "home", "working"]


def test_a_refused_install_path_gives_no_path_with_its_reason(tmp_path: Path) -> None:
    outside = str(tmp_path / "elsewhere" / "runtime-canary")
    assert canary.install_path(tmp_path, {}) == ("", "claude plugin list --json gives it no installPath")
    assert canary.install_path(tmp_path, {"installPath": "cache/x"}) == (
        "",
        "claude plugin list --json gives it a relative installPath cache/x",
    )
    assert canary.install_path(tmp_path / "home", {"installPath": outside}) == (
        "",
        f"claude plugin list --json gives it an installPath outside the throwaway home {outside}",
    )
    placed = str(tmp_path / "cache" / "runtime-canary" / "0.0.0-dev")
    assert canary.install_path(tmp_path, {"installPath": placed}) == (placed, "")


def test_a_plugin_the_canary_cannot_place_leaves_the_later_plugins_checked(tmp_path: Path) -> None:
    plugins = canary.build(tmp_path, [REPOSITORY_ROOT, FIXTURE_SOURCE])
    entry = {"id": "coding-agent-skills@canary", "version": "0.0.0-dev"}
    runner = FakeClaude(tmp_path, entries={"coding-agent-skills": entry})
    runner.plugins = plugins
    lines = list(canary.install(session(tmp_path, runner), "claude", plugins))
    refusal = quote("claude plugin list --json gives it no installPath")
    assert f"INSTALL_FAILED claude coding-agent-skills {refusal}" in lines
    assert "INSTALLED claude coding-agent-skills-dotnet 0.0.0-dev" in lines
    assert "INSTALLED claude runtime-canary 0.0.0-dev" in lines
    assert "DISCOVERED claude runtime-canary:runtime-canary-probe skill" in lines
    paths = json.loads(lines[-1].removeprefix("PATHS "))
    assert sorted(paths) == ["coding-agent-skills-dotnet", "runtime-canary"]


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


def test_a_probe_record_the_canary_will_not_read_fails_by_name_and_keeps_its_transcript(tmp_path: Path) -> None:
    installed = tmp_path / "cache" / "runtime-canary" / "0.0.0-dev"
    installed.mkdir(parents=True)
    (tmp_path / STATE).mkdir()
    records = installed.parent / STATE / "probe.jsonl"

    def oversized(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        records.write_text('{"run": ' + "1" * 5000 + "}\n", encoding="utf-8")
        return Completed(0, "", "")

    lines = canary.probe(
        canary.Session(tmp_path, oversized, {}, 30), "claude", {"runtime-canary": str(installed)}, "runtime-canary"
    )
    assert lines[0].startswith(
        f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} FAILED "cannot read what the run recorded in '
        f"{records.as_posix()}: line 1: Exceeds the limit"
    )
    assert lines[1].startswith("TRANSCRIPT claude probe ")


def test_a_probe_record_error_naming_no_file_fails_naming_the_marker_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "cache" / "runtime-canary" / "0.0.0-dev"
    installed.mkdir(parents=True)
    (tmp_path / STATE).mkdir()

    def nameless(path: Path) -> list[dict[str, Any]]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(canary, "read_records", nameless)
    lines = canary.probe(
        canary.Session(tmp_path, lambda *_: Completed(0, "", ""), {}, 30),
        "claude",
        {"runtime-canary": str(installed)},
        "runtime-canary",
    )
    records = (installed.parent / STATE / "probe.jsonl").as_posix()
    assert lines[0] == (
        f'RUNTIME claude runtime-canary:{FIXTURE_SKILL} FAILED "cannot read what the run recorded in {records}: '
        'Permission denied"'
    )


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


# -- canary() and main() ----------------------------------------------------------------------------------------


def documented_kinds() -> set[str]:
    """Each line kind the module's docstring lists under "It prints one fact per line"."""
    doc = canary.__doc__ or ""
    block = doc.split("It prints one fact per line:", 1)[1].split("\nIt exits", 1)[0]
    return set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", block))


def kinds(output: str) -> set[str]:
    return {line.split(" ", 1)[0] for line in output.splitlines() if line}


def run_canary(
    home: Path, capsys: pytest.CaptureFixture[str], runner: FakeClaude | None = None, **options: Any
) -> tuple[int, str]:
    home.mkdir(exist_ok=True)
    runner = runner or FakeClaude(home)
    code = canary.canary(home, runner=runner, which=lambda name: "claude", base={"PATH": "x"}, **options)
    return code, capsys.readouterr().out


def recorded(output: str) -> list[str]:
    lines = output.splitlines()
    return lines[lines.index("RECORD_BEGIN") + 1 : lines.index("RECORD_END")]


def test_a_discovery_run_records_the_version_and_every_judged_line_with_paths_as_placeholders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    runner = FakeClaude(home)
    real_build = canary.build

    def build(where: Path, sources: list[Path]) -> list[plugin_build.Plugin]:
        runner.plugins = real_build(where, sources)
        return runner.plugins

    monkeypatch.setattr(canary, "build", build)
    code, output = run_canary(home, capsys, runner, discovery_only=True)
    assert code == 0, output
    assert output.startswith(f"HOME {quote(home.as_posix())}\n")
    assert (home / STATE).is_dir()
    record = recorded(output)
    assert record[0].startswith("VERSION claude ")
    assert "INSTALLED claude coding-agent-skills 0.0.0-dev" in record
    assert all(line.split(" ", 1)[0] in canary.RECORDED for line in record[1:])
    assert not any(line.startswith(("BUILT", "HOME", "TRANSCRIPT")) for line in record)
    assert not any(str(home) in line or home.as_posix() in line for line in record)
    # No session started: every command the fake answered was a plugin command or the version.
    assert {arguments[1] for arguments, _ in runner.calls} == {"plugin", "--version"}


def test_nothing_checked_when_claude_is_not_installed_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    code = canary.canary(home, runner=FakeClaude(home), which=lambda name: None, base={})
    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines()[-1] == 'NOTHING_CHECKED "claude is not installed where deploy.py check looks"'


@pytest.mark.parametrize(
    ("said", "code"),
    [
        ([], 1),
        (['BUILT a "x"', "VALIDATED claude a"], 0),
        (["VALIDATED claude a", "UNDISCOVERED claude a:b skill"], 1),
        (['RUNTIME claude a:b RAN "s" "c"', "GUARD claude main-session PASSED"], 0),
        (["GUARD claude main-session PASSED", 'GUARD claude a:b DENIED "x"', 'UNGUARDED claude a:b REFUSED "x"'], 0),
        (["GUARD claude a:b PASSED"], 1),
        (['GUARD claude main-session DENIED "x"'], 1),
    ],
)
def test_the_run_fails_when_nothing_was_said_or_any_line_fails(
    said: list[str], code: int, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "checks", lambda session, executable, discovery_only: iter(said))
    found, output = run_canary(tmp_path / "home", capsys)
    assert found == code, output
    assert recorded(output)[1:] == [line for line in said if line.split(" ", 1)[0] in canary.RECORDED]


def test_main_keeps_the_home_of_a_failed_run_and_removes_one_that_passed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    homes: list[Path] = []
    real_create = create_home

    def create() -> Path:
        homes.append(real_create())
        return homes[-1]

    def passing(home: Path, **options: Any) -> int:
        claim_home(home)
        return 0

    monkeypatch.setattr(canary, "create_home", create)
    try:
        monkeypatch.setattr(canary, "canary", passing)
        assert canary.main([]) == 0
        assert capsys.readouterr().out.splitlines()[-1] == f"REMOVED {quote(homes[0].as_posix())}"
        assert not homes[0].exists()
        monkeypatch.setattr(canary, "canary", lambda home, **options: 1)
        assert canary.main([]) == 1
        assert capsys.readouterr().out.splitlines()[-1].startswith(f"HOME_KEPT {quote(homes[1].as_posix())} ")
        assert homes[1].is_dir()
    finally:
        for home in homes:
            shutil.rmtree(home, ignore_errors=True)


def test_main_reports_a_build_or_write_failure_on_one_line_and_keeps_the_home(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    homes: list[Path] = []
    real_create = create_home

    def create() -> Path:
        homes.append(real_create())
        return homes[-1]

    monkeypatch.setattr(canary, "create_home", create)
    try:
        for error in (plugin_build.BuildError("no plugin"), PermissionError("denied")):

            def failing(home: Path, error: Exception = error, **options: Any) -> int:
                raise error

            monkeypatch.setattr(canary, "canary", failing)
            assert canary.main([]) == 1
            lines = capsys.readouterr().out.splitlines()
            assert lines[0] == f"FAILED {quote(str(error))}"
            assert lines[1].startswith("HOME_KEPT ")
    finally:
        for home in homes:
            shutil.rmtree(home, ignore_errors=True)


def test_the_docstring_lists_every_kind_of_line_the_canary_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    printed: set[str] = set()
    home = tmp_path / "home"
    runner = FakeClaude(home)
    real_build = canary.build

    def build(where: Path, sources: list[Path]) -> list[plugin_build.Plugin]:
        runner.plugins = real_build(where, sources)
        return runner.plugins

    monkeypatch.setattr(canary, "build", build)
    printed |= kinds(run_canary(home, capsys, runner, discovery_only=True)[1])
    (tmp_path / "bare").mkdir()
    canary.canary(tmp_path / "bare", runner=runner, which=lambda name: None, base={})
    printed |= kinds(capsys.readouterr().out)

    def failing(home: Path, **options: Any) -> int:
        raise OSError("denied")

    monkeypatch.setattr(canary, "create_home", lambda: tmp_path / "kept")
    monkeypatch.setattr(canary, "canary", failing)
    canary.main([])
    printed |= kinds(capsys.readouterr().out)
    assert {"HOME", "BUILT", "VALIDATED", "INSTALLED", "DISCOVERED", "VERSION", "NOTHING_CHECKED", "FAILED"} <= printed
    assert printed - documented_kinds() == set()


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_a_timeout_that_is_no_limit_is_a_usage_error(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        canary.main(["--timeout", value])
    assert exited.value.code == 2
    refusal = capsys.readouterr().err.splitlines()[-1]
    assert "argument --timeout: " in refusal
    assert refusal.endswith(f" seconds, not {value}")

"""Install the Claude Code plugins into a throwaway configuration and check that Claude Code finds and runs them.

Usage:
  python tools/plugin_canary.py [--discovery-only] [--timeout SECONDS] [--keep-home]
  python tools/plugin_canary.py --remove-home DIR

It makes a fresh home under the temporary directory, as tools/runtime_canary.py does, and assembles into it, with
tools/plugin_build.py, this checkout's plugins and the plugin of the fixture source in tests/fixtures/runtime-canary.
It writes a marketplace beside them and, with CLAUDE_CONFIG_DIR set to a folder of the home, so nothing reaches the
user's own configuration, validates each plugin, adds the marketplace, installs each plugin, giving each option its
manifest declares a folder of the home, and reads what Claude Code installed with `claude plugin list --json` and
`claude plugin details`. None of that starts a model.

Unless --discovery-only is given, it then starts Claude Code three times, signed in as the user is, with the installed
copies loaded through --plugin-dir for that session alone and project settings only, as the runtime canary does:

- the fixture's probe, `/runtime-canary:runtime-canary-probe`, which must run the installed copy's script;
- the plugin's reviewer agent, which the plugin's hooks file must guard though a plugin agent's own hooks are
  ignored: the session reads a marker file outside any review run itself, which must pass, and starts the reviewer
  with a reviewer's task naming the same file as its prompt, whose read the guard must deny, since no review run
  prepared it;
- the plugin's reviewer again, with every hook off as `disableAllHooks` turns them off, on the one role of a review run
  the canary makes in the home: the reviewer writes that role's result with no guard to claim the role, and the
  pipeline's `check` must refuse the result, as `unguarded_roles` in review_dispatch.py decides it.

Each check is judged from the session's stream, never from what the model reports. It prints one fact per line:

  HOME "<dir>"                                        the throwaway home
  BUILT <plugin> "<path>"                             a plugin assembled into the home
  VALIDATED claude <plugin>                           or VALIDATION_FAILED claude <plugin> "<reason>"
  INSTALLED claude <plugin> <version>                 or INSTALL_FAILED claude <plugin> "<reason>"
  DISCOVERED claude <plugin>:<item> <kind>            a skill, agent, or hook event the installed plugin lists
  UNDISCOVERED claude <plugin>:<item> <kind>          one the plugin carries that it does not list
  RUNTIME claude <plugin>:<skill> RAN "<script>" "<cwd>"   or FAILED "<reason>"
  GUARD claude main-session PASSED                    or DENIED "<reason>" or FAILED "<reason>"
  GUARD claude <plugin>:<agent> DENIED "<reason>"     or PASSED, which fails the run, or FAILED "<reason>"
  UNGUARDED claude <plugin>:<agent> REFUSED "<reason>"   or ACCEPTED, which fails the run, or FAILED "<reason>"
  TRANSCRIPT claude <check> "<path>"                  the session's stream
  RECORD_BEGIN ... RECORD_END                         the lines a pull request records, paths as placeholders
  REMOVED "<dir>" or HOME_KEPT "<dir>" "<reason>"     as tools/runtime_canary.py ends

It exits 1 when any check fails. This is a manual check, never part of tests/run_validation.py: each started session
calls a model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import skill_menu
from console import run_main
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

from review_dispatch import REVIEWER_TASK, unguarded_roles
from review_guard import REVIEWER, RUN_FILE, RUN_PREFIX
from review_run import RunState

from deployer import platform_support
from tools import plugin_build
from tools.runtime_canary import (
    DEFAULT_TIMEOUT,
    FIXTURE_SKILL,
    FIXTURE_SOURCE,
    INSTRUCTION,
    REPOSITORY_ROOT,
    RUN_ID,
    STATE,
    Completed,
    RecordRefused,
    Runner,
    claim_home,
    create_home,
    discovery_failure,
    environment,
    finish,
    forward,
    inside,
    marked,
    placeholders,
    places,
    quote,
    read_records,
    remove_home,
    run_process,
    runtime_version,
    unreadable,
)

MARKETPLACE = "canary"
CONFIG = "claude-config"
# The folder of the home holding a folder for each option a plugin declares, as its value at install.
OPTIONS = "options"
GUARD_MARKER = "PLUGIN-CANARY-OUTSIDE"
# The run the reviewer works in with every hook off, its one role, and the settings that turn the hooks off.
UNGUARDED_RUN = f"{RUN_PREFIX}unguarded"
UNGUARDED_ROLE = "canary"
HOOKS_OFF = json.dumps({"disableAllHooks": True})
BOUNDARY = "Code-review reviewer boundary"
DETAILS = re.compile(r"^\s*(Skills|Agents|Hooks)\s+\(\d+\)\s*(.*)$")
RECORDED = (
    "VALIDATED",
    "VALIDATION_FAILED",
    "INSTALLED",
    "INSTALL_FAILED",
    "DISCOVERED",
    "UNDISCOVERED",
    "RUNTIME",
    "GUARD",
    "UNGUARDED",
)
FAILING = ("VALIDATION_FAILED", "INSTALL_FAILED", "UNDISCOVERED")


@dataclass(frozen=True)
class Session:
    """What every command of one canary run shares."""

    home: Path
    runner: Runner
    base: Mapping[str, str]
    timeout: float

    @property
    def config(self) -> dict[str, str]:
        """The environment of a command that must read and write only the throwaway configuration."""
        return {**self.base, "CLAUDE_CONFIG_DIR": str(self.home / CONFIG)}


def build(home: Path, sources: list[Path]) -> list[plugin_build.Plugin]:
    """Assemble each source's plugins into the home, and write the marketplace that lists them by folder."""
    plugins: list[plugin_build.Plugin] = []
    entries = []
    for source_dir in sources:
        catalogue = plugin_build.load_catalogue(source_dir)
        source = plugin_build.load_source(source_dir)
        assembly = plugin_build.assemble(source, catalogue, readme=plugin_build.load_readme(source_dir))
        plugin_build.write(assembly, home, archive=False)
        for plugin in assembly.plugins:
            plugins.append(plugin)
            entry = {key: value for key, value in catalogue[plugin.name].items() if key != "source"}
            entries.append({**entry, "source": f"./{plugin.name}"})
    marketplace = {"name": MARKETPLACE, "description": "The plugin canary's", "owner": {"name": "canary"}}
    (home / ".claude-plugin").mkdir()
    (home / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({**marketplace, "plugins": entries}, indent=2), encoding="utf-8"
    )
    return plugins


def last_line(completed: Completed) -> str:
    lines = [line.strip() for line in (completed.stdout + completed.stderr).splitlines() if line.strip()]
    return completed.error or (lines[-1] if lines else f"exited with code {completed.returncode}")


def details(text: str) -> dict[str, set[str]]:
    """The component inventory `claude plugin details` prints: skill and agent names, and hook event names."""
    found: dict[str, set[str]] = {"skill": set(), "agent": set(), "hook": set()}
    for line in text.splitlines():
        match = DETAILS.match(line)
        if match:
            kind = {"Skills": "skill", "Agents": "agent", "Hooks": "hook"}[match.group(1)]
            names = match.group(2).split("(", 1)[0]
            found[kind].update(name.strip() for name in re.split(r"[,\s]+", names) if name.strip())
    return found


def expected(plugin: plugin_build.Plugin) -> dict[str, set[str]]:
    hooks = (
        json.loads(plugin.files[skill_menu.PLUGIN_HOOKS]).get("hooks", {})
        if skill_menu.PLUGIN_HOOKS in plugin.files
        else {}
    )
    return {"skill": set(plugin.skills), "agent": set(plugin.agents), "hook": set(hooks)}


def option_values(home: Path, plugin: plugin_build.Plugin) -> list[str]:
    """The --config arguments giving each option the plugin's manifest declares a new folder of the home, as
    `claude plugin install` takes them: a required option left unset would stop the install."""
    declared = json.loads(plugin.files[skill_menu.PLUGIN_MANIFEST]).get("userConfig", {})
    arguments: list[str] = []
    for name in sorted(declared):
        folder = home / OPTIONS / name
        folder.mkdir(parents=True, exist_ok=True)
        arguments += ["--config", f"{name}={folder}"]
    return arguments


def install_path(home: Path, entry: Mapping[str, Any]) -> tuple[str, str]:
    """The installed copy's folder from its `claude plugin list --json` entry, and a reason to refuse it unless it is an
    absolute path whose parent folder is inside the throwaway home, since the probe writes beside the copy."""
    path = entry.get("installPath")
    if not isinstance(path, str) or not path:
        return "", "claude plugin list --json gives it no installPath"
    installed = Path(path)
    if not installed.is_absolute():
        return "", f"claude plugin list --json gives it a relative installPath {path}"
    if not inside(str(installed.parent), home):
        return "", f"claude plugin list --json gives it an installPath outside the throwaway home {path}"
    return path, ""


def install(session: Session, executable: str, plugins: list[plugin_build.Plugin]) -> Iterator[str]:
    """Validate, install, and list each plugin in the throwaway configuration; the installed copies' paths are yielded
    last, as one `PATHS` line of JSON, for the sessions that load them."""
    for plugin in plugins:
        result = session.runner(
            [executable, "plugin", "validate", "--strict", str(session.home / plugin.name)],
            session.home,
            session.config,
            session.timeout,
        )
        ok = result.returncode == 0 and not result.error
        yield (
            f"VALIDATED claude {plugin.name}"
            if ok
            else f"VALIDATION_FAILED claude {plugin.name} {quote(last_line(result))}"
        )
    added = session.runner(
        [executable, "plugin", "marketplace", "add", str(session.home)], session.home, session.config, session.timeout
    )
    paths: dict[str, str] = {}
    for plugin in plugins:
        identifier = f"{plugin.name}@{MARKETPLACE}"
        result = added
        if added.returncode == 0 and not added.error:
            result = session.runner(
                [executable, "plugin", "install", identifier, *option_values(session.home, plugin)],
                session.home,
                session.config,
                session.timeout,
            )
        if result.returncode != 0 or result.error:
            yield f"INSTALL_FAILED claude {plugin.name} {quote(last_line(result))}"
            continue
        listing = session.runner(
            [executable, "plugin", "list", "--json"], session.home, session.config, session.timeout
        )
        try:
            installed = next(item for item in json.loads(listing.stdout) if item.get("id") == identifier)
        except (ValueError, StopIteration, AttributeError):
            yield f"INSTALL_FAILED claude {plugin.name} {quote('claude plugin list --json does not list it')}"
            continue
        path, refusal = install_path(session.home, installed)
        if refusal:
            yield f"INSTALL_FAILED claude {plugin.name} {quote(refusal)}"
            continue
        paths[plugin.name] = path
        yield f"INSTALLED claude {plugin.name} {installed.get('version', 'unknown')}"
        shown = session.runner(
            [executable, "plugin", "details", identifier], session.home, session.config, session.timeout
        )
        listed = details(shown.stdout)
        for kind, names in expected(plugin).items():
            for name in sorted(names):
                known = name in listed[kind]
                yield f"{'DISCOVERED' if known else 'UNDISCOVERED'} claude {plugin.name}:{name} {kind}"
    yield f"PATHS {json.dumps(paths)}"


def stream_events(output: str) -> Iterator[dict[str, Any]]:
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def result_text(content: object) -> str:
    """A tool result's content as text: a string as it is, or the text of each part of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(result_text(part.get("text") if isinstance(part, dict) else part) for part in content)
    return ""


def tool_results(output: str) -> Iterator[tuple[str | None, bool, str]]:
    """Each tool result the stream holds: the tool call of the subagent it came back to, or None for the main
    session's own call, whether it is an error, and its text."""
    for event in stream_events(output):
        message = event.get("message")
        if event.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                parent = event.get("parent_tool_use_id")
                yield (
                    parent if isinstance(parent, str) else None,
                    bool(item.get("is_error")),
                    result_text(item.get("content")),
                )


def denial(text: str) -> str:
    """The guard's reason, from the boundary's name to the end of its sentence."""
    reason = text[text.index(BOUNDARY) :]
    return reason.split(";", 1)[0].split("\n", 1)[0]


def guard_lines(output: str, agent: str, marker: str) -> list[str]:
    """Whether the main session read the marker file and the reviewer was denied it, from the session's stream."""
    results = list(tool_results(output))
    main = [(error, text) for parent, error, text in results if parent is None]
    reviewer = [(error, text) for parent, error, text in results if parent is not None]
    if any(marker in text and not error for error, text in main):
        lines = ["GUARD claude main-session PASSED"]
    elif any(BOUNDARY in text for _, text in main):
        lines = [f"GUARD claude main-session DENIED {quote(denial(next(t for _, t in main if BOUNDARY in t)))}"]
    else:
        lines = [f"GUARD claude main-session FAILED {quote('the session never read the marker file')}"]
    if any(marker in text and not error for error, text in reviewer):
        lines.append(f"GUARD claude {agent} PASSED")
    elif any(error and BOUNDARY in text for error, text in reviewer):
        lines.append(
            f"GUARD claude {agent} DENIED {quote(denial(next(t for e, t in reviewer if e and BOUNDARY in t)))}"
        )
    else:
        lines.append(f"GUARD claude {agent} FAILED {quote('the reviewer made no call the guard answered')}")
    return lines


def session_command(executable: str, prompt: str, paths: Mapping[str, str]) -> list[str]:
    """A session loading each installed plugin for itself alone. The probe's own grants come from its skill; a guard
    session adds its own grants after these arguments."""
    loaded = [item for path in paths.values() for item in ("--plugin-dir", path)]
    return [
        executable,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
        *loaded,
    ]


def guard_grants() -> list[str]:
    """What the guard session may do without asking: read, and start the reviewer."""
    return ["Read", "Agent"]


def unguarded_grants() -> list[str]:
    """What the hooks-off session may do without asking: read, start the reviewer, and write its result."""
    return ["Read", "Write", "Agent"]


def guard_command(executable: str, prompt: str, paths: Mapping[str, str]) -> list[str]:
    """The guard session: a plugin session granted guard_grants."""
    return [*session_command(executable, prompt, paths), "--allowedTools", *guard_grants()]


def unguarded_command(executable: str, request: str, paths: Mapping[str, str]) -> list[str]:
    """The hooks-off session: a plugin session granted unguarded_grants, with every hook off."""
    return [
        *session_command(executable, request, paths),
        "--allowedTools",
        *unguarded_grants(),
        "--settings",
        HOOKS_OFF,
    ]


def transcript(session: Session, name: str, completed: Completed) -> str:
    path = session.home / STATE / "transcripts" / f"claude-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(completed.stdout, encoding="utf-8")
    return f"TRANSCRIPT claude {name} {quote(forward(path))}"


def probe(session: Session, executable: str, paths: Mapping[str, str], fixture: str) -> list[str]:
    """Start the fixture's probe from its installed copy, whose script records the run beside that copy."""
    installed = Path(paths[fixture])
    state = installed.parent / STATE
    state.mkdir(exist_ok=True)
    (state / "probe.jsonl").write_text("", encoding="utf-8")
    run = secrets.token_hex(16)
    (state / RUN_ID).write_text(run, encoding="utf-8")
    skill = f"{fixture}:{FIXTURE_SKILL}"
    command = session_command(executable, f"/{skill} {INSTRUCTION}", paths)
    script_log = session.home / STATE / "scripts" / "claude-probe.jsonl"
    script_log.parent.mkdir(parents=True, exist_ok=True)
    completed = session.runner(
        command, session.home, environment(session.base, script_log, session.home), session.timeout
    )
    script = installed / "skills" / FIXTURE_SKILL / "scripts" / "canary_probe.py"
    unread = ""
    try:
        records = [record for record in marked(read_records(state / "probe.jsonl"), run) if record.get("script")]
    except (OSError, RecordRefused) as exc:
        records, unread = [], unreadable(exc, state / "probe.jsonl")
    ran = [record for record in records if Path(record["script"]).resolve() == script.resolve()]
    if unread:
        outcome = f"FAILED {quote(unread)}"
    elif ran:
        outcome = f"RAN {quote(forward(ran[0]['script']))} {quote(forward(ran[0].get('cwd', '')))}"
    elif nothing := discovery_failure(completed.stdout):
        outcome = f"FAILED {quote(nothing)}"
    elif completed.timed_out:
        outcome = f"FAILED {quote(f'timed out after {session.timeout:g} seconds before the probe ran')}"
    else:
        outcome = f"FAILED {quote('the installed copy of the probe never ran: ' + last_line(completed))}"
    return [f"RUNTIME claude {skill} {outcome}", transcript(session, "probe", completed)]


def guard(session: Session, executable: str, paths: Mapping[str, str], plugin: str) -> list[str]:
    """Read a marker file outside every review run in the main session, then have the plugin's reviewer read it."""
    marker = session.home / "outside.txt"
    text = f"{GUARD_MARKER}-{secrets.token_hex(4)}"
    marker.write_text(f"{text}\n", encoding="utf-8")
    agent = f"{plugin}:{REVIEWER}"
    # The reviewer is given a reviewer's task, naming the marker as its prompt file, so it reads the file as a
    # reviewer reads its prompt, and the guard, not the agent's own instructions, decides the read.
    task = REVIEWER_TASK.format(prompt=marker)
    prompt = (
        f"First read the file {marker} yourself with the Read tool. Then start one subagent with the Agent tool, "
        f"subagent_type {agent}, with exactly this prompt: {task} Then stop and report what it replied. This is a "
        "plugin canary session."
    )
    command = guard_command(executable, prompt, paths)
    script_log = session.home / STATE / "scripts" / "claude-guard.jsonl"
    completed = session.runner(
        command, session.home, environment(session.base, script_log, session.home), session.timeout
    )
    return [*guard_lines(completed.stdout, agent, text), transcript(session, "guard", completed)]


def unguarded(session: Session, executable: str, paths: Mapping[str, str], plugin: str) -> list[str]:
    """Have the plugin's reviewer work a review run's one role with every hook off, then hold its result to the guard's
    claim as `check` does: with no hook, no guard claimed the role, so the result is refused."""
    run = session.home / UNGUARDED_RUN
    run.mkdir(exist_ok=True)
    prompt, result = run / f"{UNGUARDED_ROLE}.prompt.md", run / f"{UNGUARDED_ROLE}.json"
    prompt.write_text(
        f'Write the JSON text {{"canary": true}} to {result} with the Write tool. Do nothing else.\n', encoding="utf-8"
    )
    role = {"id": UNGUARDED_ROLE, "prompt_file": str(prompt), "result_file": str(result)}
    (run / RUN_FILE).write_text(json.dumps({"roles": [role]}), encoding="utf-8")
    agent = f"{plugin}:{REVIEWER}"
    task = REVIEWER_TASK.format(prompt=prompt)
    request = (
        f"Start one subagent with the Agent tool, subagent_type {agent}, with exactly this prompt: {task} Then stop "
        "and report what it replied. This is a plugin canary session."
    )
    command = unguarded_command(executable, request, paths)
    script_log = session.home / STATE / "scripts" / "claude-unguarded.jsonl"
    completed = session.runner(
        command, session.home, environment(session.base, script_log, session.home), session.timeout
    )
    if not result.is_file():
        outcome = f"FAILED {quote('the reviewer wrote no result: ' + last_line(completed))}"
    else:
        # The state check reads: a native subagent's run on Claude Code, which the guard should have claimed.
        state = cast(RunState, {"runtime": "claude-code", "dispatch": "subagents", "reads": {}})
        refused = unguarded_roles(run, state, [UNGUARDED_ROLE])
        outcome = f"REFUSED {quote(refused[UNGUARDED_ROLE])}" if refused else "ACCEPTED"
    return [f"UNGUARDED claude {agent} {outcome}", transcript(session, "unguarded", completed)]


def checks(session: Session, executable: str, discovery_only: bool) -> Iterator[str]:
    plugins = build(session.home, [REPOSITORY_ROOT, FIXTURE_SOURCE])
    yield from (f"BUILT {plugin.name} {quote(forward(session.home / plugin.name))}" for plugin in plugins)
    paths: dict[str, str] = {}
    for line in install(session, executable, plugins):
        if line.startswith("PATHS "):
            paths = json.loads(line[len("PATHS ") :])
        else:
            yield line
    if discovery_only:
        return
    fixture = skill_menu.plugin_name(plugin_build.load_source(FIXTURE_SOURCE).source_id)
    reviewers = [plugin.name for plugin in plugins if REVIEWER in plugin.agents]
    if fixture not in paths or not reviewers or reviewers[0] not in paths:
        yield f"RUNTIME claude {fixture}:{FIXTURE_SKILL} FAILED {quote('a plugin the sessions load is not installed')}"
        return
    yield from probe(session, executable, paths, fixture)
    yield from guard(session, executable, paths, reviewers[0])
    yield from unguarded(session, executable, paths, reviewers[0])


def failed(line: str) -> bool:
    kind = line.split(" ", 1)[0]
    if kind in FAILING:
        return True
    if kind == "RUNTIME":
        return " RAN " not in line
    if kind == "UNGUARDED":
        return " REFUSED " not in line
    if kind == "GUARD":
        main = " main-session " in line
        return not (" PASSED" in line if main else " DENIED " in line)
    return False


def canary(
    home: Path,
    *,
    runner: Runner = run_process,
    which: Callable[[str], str | None] = platform_support.find_executable,
    base: Mapping[str, str] | None = None,
    discovery_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    print(f"HOME {quote(forward(home))}", flush=True)
    claim_home(home)
    executable = which("claude")
    if executable is None:
        print(f"NOTHING_CHECKED {quote('claude is not installed where deploy.py check looks')}")
        return 1
    session = Session(home, runner, dict(os.environ if base is None else base), timeout)
    said: list[str] = []
    for line in checks(session, executable, discovery_only):
        print(line, flush=True)
        said.append(line)
    version = runtime_version(executable, runner, session.base, home)
    paths = places(home)
    print("RECORD_BEGIN", flush=True)
    print(f"VERSION claude {version}")
    for line in said:
        if line.split(" ", 1)[0] in RECORDED:
            print(placeholders(line, paths))
    print("RECORD_END", flush=True)
    return 1 if not said or any(failed(line) for line in said) else 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/plugin_canary.py",
        description="Install the plugins into a throwaway configuration and check that Claude Code runs them.",
    )
    parser.add_argument("--discovery-only", action="store_true", help="install and list the plugins; run no model")
    parser.add_argument("--remove-home", metavar="DIR", help="delete a home an earlier run printed as HOME, and exit")
    parser.add_argument("--keep-home", action="store_true", help="keep the home after a run that passed")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    options = parser.parse_args(arguments)
    if options.remove_home is not None:
        return remove_home(options.remove_home)
    home = create_home()
    try:
        code = canary(home, discovery_only=options.discovery_only, timeout=options.timeout)
    except (plugin_build.BuildError, OSError) as exc:
        print(f"FAILED {quote(str(exc))}")
        code = 1
    return finish(home, code, options.keep_home)


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")

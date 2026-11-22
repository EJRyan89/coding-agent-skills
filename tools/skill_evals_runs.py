"""Running a skill's evaluation scenarios: each model's throwaway home, the Claude Code session, and what each run
shows of the models it used."""

from __future__ import annotations

import concurrent.futures
import contextlib
import itertools
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import file_presence
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import frontmatter
import json_writer
import whole_number
from git_client import NO_HOOKS, GitClient, GitError
from review_archive import ArchiveError, latest_record, list_versions
from review_io import PersistenceError
from search_boundary import CLAUDE_INSTRUCTIONS_OFF_VARIABLES
from text_lines import LINE_BREAK, text_lines

from deployer import fsops, tools
from deployer.platform_support import PYTHON
from tools import claude_stream, runtime_canary
from tools.runtime_canary import Completed, Runner
from tools.skill_evals_records import (
    FAILED_CALL,
    MODELS,
    NO_SYNTHESIS,
    RECORD,
    REVIEWER_EFFORT,
    RUN,
    SESSION_CALLS,
    Index,
    Record,
    RecordError,
    RoutePin,
    insights_report,
    names_model,
    pinned_models,
    review,
    unguarded,
)
from tools.skill_evals_scenarios import (
    BY_NAME_FOLDER,
    CHECKOUT_DECOY,
    FLAGS,
    HIDDEN_DECOY,
    REPOSITORY_ROOT,
    SYNTHESIS,
    SYNTHESIS_FOLDERS,
    SYNTHESIS_SKILL,
    TRIGGER,
    Scenario,
    document_arguments,
    document_place,
    judged_calls,
    quote,
    reviewed_fixture,
    skill_arguments,
)

# The session's own model, which orchestrates the skill: the strongest, so the orchestration never limits a result.
SESSION_MODEL = list(MODELS)[-1]
# The reviewer agent the runs set the model of, in each model's home, and the guard its hook runs: the hook names
# the profile folder's copy, and the definition the session gets names the home's.
REVIEWER_AGENT = Path(".claude") / "agents" / "code-review-reviewer.md"
# A home's deployed skills folder, and the core whose scripts a reviewer runs, by its folder's name there.
SKILLS = Path(".claude") / "skills"
CORE = "ejryan89-code-review-core"
GUARD = SKILLS / CORE / "scripts" / "review_guard.py"
PROFILE_GUARD = f"~/{GUARD.as_posix()}"
# The file in each home holding the reviewer definition the session gets with --agents, and the frontmatter keys
# that definition carries over; an agent with any other key is refused, since its definition would drop it.
AGENTS = "agents.json"
AGENT_KEYS = frozenset({"name", "description", "tools", "model", "effort", "omitClaudeMd", "hooks"})
# The variables whose effort outranks an agent's own `effort`, which a run leaves out so the reviewer's stated effort
# holds; a module-level *_VARIABLES collection, so tools/runtime_changelogs.py watches the changelogs for them.
EFFORT_VARIABLES = ("CLAUDE_CODE_EFFORT_LEVEL",)
# The variable that keeps a session from writing auto memory as well as loading it: "When disabled, Claude does not
# create or load auto memory files" (CLAUDE_CODE_DISABLE_AUTO_MEMORY, https://code.claude.com/docs/en/env-vars, and
# "Enable or disable auto memory", https://code.claude.com/docs/en/memory). CLAUDE_INSTRUCTIONS_OFF_VARIABLES stops
# only the loading, so without it a run could still save memories into the user's own memory folder for the home,
# which every run on that model shares. Also a *_VARIABLES collection the changelog watch reads.
AUTO_MEMORY_OFF_VARIABLES = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# No run writes `claudeMdExcludes` (https://code.claude.com/docs/en/memory, "Exclude specific CLAUDE.md files"):
# CLAUDE_INSTRUCTIONS_OFF_VARIABLES' CLAUDE_CODE_DISABLE_CLAUDE_MDS already keeps every CLAUDE.md out, "including
# user, project, and auto memory files" (https://code.claude.com/docs/en/env-vars), and the managed one too, which
# the memory page says claudeMdExcludes cannot exclude, so a list of excluded files would repeat a narrower part of it.
# What the home's path may not hold: the hook command quotes it for Python inside a Bash double quote.
UNQUOTABLE = re.compile(r"[\\'\"`$!]")
# The review version a run's record must be, by the scenario's mode: a re-review's canary root first holds the prior
# record as version 1.
RECORD_VERSION = {"initial": 1, "re-review": 2}
STATE = ".skill-evals"
# A run that recorded no review, as the results file records it: why the record is missing may name the machine's
# paths, which the FAIL lines keep.
NO_REVIEW = "the run recorded no review"
# The record each of a trigger scenario's runs writes in its folder, and the turns a request's session may take: enough
# to read a file before choosing, few enough that a session which starts a skill stops soon after, since the start is
# all that is judged.
TRIGGER_RECORD = "trigger.json"
TRIGGER_TURNS = 2
# A synthesis run's own code-review configuration, written in its folder, and the reports its summaries folder may
# hold, one per repository set and period, of which the run's own is the only one.
SYNTHESIS_CONFIG = "config.json"
SUMMARIES = "summaries"
REPORT_PATTERN = f"{SUMMARIES}/*/*/insights.json"
# The session's own command that records a synthesis, as review-insights' step 2 gives it, which a run counts only
# when it names Claude Code as the host and is not --inline, so `synthesize` recorded the result only once the reviewer
# guard had claimed its role.
RECORDING = re.compile(r"review_insights\.py\S*\s+synthesize\b")
GUARDED_HOST = re.compile(r"--host[= ]+[\"']?claude-code\b")
INLINE = re.compile(r"--inline\b")
RunKey = tuple[str, str, Index]
# A run waiting for its turn: its scenario, its model, the model's home, and which run it is.
Queued = tuple[Scenario, str, Path, Index]
CONTENT_ERRORS_ALLOWED = {
    "review_run": (
        "the only content it writes is a synthesis run's configuration, built from the run's own paths and the "
        "repository the scenario loader checked, which json_writer has nothing to refuse in"
    ),
}
RecordSource = Callable[[Scenario, str, Index], Record]


def indexes(runs: int) -> list[Index]:
    """The runs of each scenario on each model: one with no number, or several numbered from 1."""
    return [None] if runs == 1 else list(range(1, runs + 1))


def run_token(index: Index) -> str:
    """What a line about one run adds after its model: nothing for a single run, else which run it is."""
    return "" if index is None else f" run={index}"


@dataclass(frozen=True)
class Run:
    """One scenario run on one model: the record it wrote, or why it counts for nothing, and the models it used.
    `recorded` is that reason as the results file records it, in fixed phrases that name no path of the machine.
    `reviewers` are the models of the part judged: the reviewers, or a trigger scenario's sessions."""

    record: Record | None
    failure: str | None
    reviewers: frozenset[str]
    session: frozenset[str]
    recorded: str | None = None
    # RUN or RECORD when the run counts for nothing; None when it counts.
    kind: str | None = None
    # A by-name run that recorded no review: the session's own tool calls when unrecorded_calls finds them in its
    # stream, which the `resolved` kind still judges; None otherwise.
    calls: list[dict[str, Any]] | None = None


def review_config(directory: Path) -> dict[str, Any]:
    """The code-review configuration every run reads. A fixture is reviewed by the generic reviewer, or by the
    manifest its pull.json names, whatever is configured, so the one repository is a placeholder; only the runtime and
    the verdict policy matter."""
    generic = {"id": "generic", "protocol_version": 1, "trusted_ref": None, "scope": "generic", "manifest_path": None}
    return {
        "schema_version": 1,
        "default_repository_set": "evaluations",
        "repository_sets": {"evaluations": ["example/unrelated"]},
        "repositories": {"example/unrelated": {"reviewer": generic, "checkout_path": None}},
        "archive_root": str(directory / "archive"),
        "local_mirror_root": None,
        "summary_root": str(directory / "summaries"),
        "dashboard_file": str(directory / "dashboard.md"),
        "github_login": "reviewer",
        "runtime": "claude-code",
        "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        "dashboard": {},
    }


def synthesis_config(run: Path, repository: str) -> dict[str, Any]:
    """A synthesis run's own code-review configuration: the run's copy of the fixture's archive and checkout, its
    summaries in the run's folder, and the one repository, reviewed by the generic reviewer, since the records name
    the reviewers that wrote them and the configured one is never read."""
    generic = review_config(run)["repositories"]["example/unrelated"]["reviewer"]
    return {
        **review_config(run),
        "repository_sets": {"evaluations": [repository]},
        "repositories": {repository: {"reviewer": generic, "checkout_path": str(run / "checkout")}},
        "archive_root": str(run / "archive"),
        "summary_root": str(run / SUMMARIES),
    }


def stage_synthesis(scenario: Scenario, run: Path) -> list[str]:
    """Copy a synthesis scenario's archive, checkout, and flag store into the run's folder, the store where the run's
    environment names it, write the run's configuration there, and return what the skill is started with: the date
    range and the repository. The fixture itself is never written."""
    period, repository = synthesis_scope(scenario)
    for folder in SYNTHESIS_FOLDERS:
        shutil.copytree(scenario.directory / folder, run / folder)
    shutil.copyfile(scenario.directory / FLAGS, run / FLAGS)
    json_writer.write(run / SYNTHESIS_CONFIG, synthesis_config(run, repository), indent=2)
    return [*period, repository]


def find_synthesis(run: Path, scenario: Scenario) -> Record:
    """The insights report a synthesis run wrote in its summaries folder, when it covers the scenario's period and
    repository and its synthesis is recorded. Raise RecordError when it wrote none or more than one, or the one it
    wrote is not the scenario's or its synthesis is not recorded."""
    reports = sorted(file_presence.matching(run, REPORT_PATTERN))
    if not reports:
        raise RecordError(f"{NO_SYNTHESIS}: it wrote no insights report")
    if len(reports) > 1:
        raise RecordError(f"{NO_SYNTHESIS}: it wrote {len(reports)} insights reports, so which is its own is unknown")
    return insights_report(reports[0], *synthesis_scope(scenario))


def synthesis_scope(scenario: Scenario) -> tuple[tuple[str, str], str]:
    """A synthesis scenario's period and repository."""
    if scenario.period is None or scenario.repository is None:
        raise ValueError(f"{scenario.name} is no synthesis scenario")
    return scenario.period, scenario.repository


def recording_commands(output: str) -> list[str]:
    """The session's own Bash or PowerShell commands that record a synthesis, `synthesize` without `--check`; a
    subagent's self-check is not one."""
    commands = []
    for event in claude_stream.events(output):
        if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            tool_input = block.get("input") if isinstance(block, dict) and block.get("type") == "tool_use" else None
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str) and RECORDING.search(command) and "--check" not in command:
                commands.append(command)
    return commands


def synthesis_unguarded(output: str) -> str | None:
    """Why a recorded synthesis was not one the reviewer guard held, or None: the session recorded it with a command
    naming Claude Code as its host and without --inline, which `synthesize` records only once the guard claimed the
    synthesis role, and with no other kind. The report keeps no trace of the guard, and its run is removed once the
    result is recorded, so the session's commands are what shows it."""
    commands = recording_commands(output)
    if not commands:
        return "the session ran no command that records the synthesis, so no reviewer guard is shown holding it"
    # A fixed phrase, which names no path of the machine, since the results file records it as it is.
    if any(INLINE.search(command) or not GUARDED_HOST.search(command) for command in commands):
        return "the synthesis was recorded inline or for another host, which no reviewer guard held"
    return None


# A session's own message, or a tool call in it, in a shape the evaluation does not read, whose calls cannot then be
# judged: reading it as no call would pass a session that searched for its document.
UNREAD_CALLS = "the session's stream holds a message or a tool call in a shape the evaluation does not read"


def session_tool_calls(output: str) -> list[dict[str, Any]]:
    """The tool calls the session's own messages made, each its tool's name and input, in order, and FAILED_CALL true
    on each whose tool result the session got reports an error; a subagent's call is not the session's. A message of
    the session's whose content is not a list of blocks, or a call whose name is not text or whose input is not an
    object, raises StreamShapeError; a tool result is read only where it is an object naming its call's ID."""
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in claude_stream.events(output):
        if event.get("parent_tool_use_id"):
            continue
        if event.get("type") == "user":
            for block in _content(event):
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") is True:
                    failed = by_id.get(str(block.get("tool_use_id")))
                    if failed is not None:
                        failed[FAILED_CALL] = True
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
            raise StreamShapeError(UNREAD_CALLS)
        for block in content:
            if block.get("type") != "tool_use":
                continue
            if not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict):
                raise StreamShapeError(UNREAD_CALLS)
            calls.append(call := {"name": block["name"], "input": block["input"]})
            if isinstance(block.get("id"), str):
                by_id[block["id"]] = call
    return calls


@contextlib.contextmanager
def named_document(scenario: Scenario, home: Path, git: GitClient) -> Iterator[list[str]]:
    """Stage a by-name document scenario's document in the model's home, the session's working directory, one folder
    down in BY_NAME_FOLDER, with a copy under the same name in each decoy `resolve` passes over: HIDDEN_DECOY, a hidden
    folder, and CHECKOUT_DECOY, a new git checkout of its own. Yield what the skill is started with, the document's file
    name alone and its stage, if it names one, and remove the three folders when the block ends, however it ends. A
    folder already there is refused, never reused, since another run's copy in it would be a second match."""
    if scenario.document is None or not scenario.by_name:
        raise ValueError(f"{scenario.name} is no by-name document scenario")
    made: list[Path] = []
    try:
        for name in (BY_NAME_FOLDER, HIDDEN_DECOY, CHECKOUT_DECOY):
            folder = home / name
            folder.mkdir()
            made.append(folder)
            folder.joinpath(scenario.document.name).write_bytes(scenario.document.read_bytes())
        # Hooks read from the null device, which holds none, so neither the user's nor a template's run.
        git.output(["-c", f"core.hooksPath={NO_HOOKS}", "init", "--quiet"], directory=home / CHECKOUT_DECOY)
        yield document_arguments(scenario, scenario.document.name)
    finally:
        for folder in made:
            fsops.remove(folder)


def prompt(skill: str, arguments: Sequence[str]) -> str:
    """The skill started by name with its arguments, each path quoted."""
    return " ".join([f"/{skill}", *(item if item.startswith("--") else f'"{item}"' for item in arguments)])


def reviewer_script_folders(home: Path, skill: str) -> list[str]:
    """Each spelling of the home's deployed core scripts folder a role's prompt may give, ending in its separator: the
    started skill's ${CLAUDE_SKILL_DIR}/../ejryan89-code-review-core/scripts/ as Claude Code fills it in, which the
    pipeline repeats from its start, and the folder's resolved path, which it gives when it cannot repeat the start."""
    skills = home / SKILLS
    started = f"{runtime_canary.forward(skills / skill)}/../{CORE}/scripts/"
    return list(dict.fromkeys([started, f"{skills / CORE / 'scripts'}{os.sep}"]))


def skill_scripts_folder(home: Path, skill: str) -> str:
    """The home's deployed scripts folder of a skill, resolved and ending in its separator, as a script that names
    itself by its resolved path, such as review-insights' synthesis self-check, spells it."""
    return f"{home / SKILLS / skill / 'scripts'}{os.sep}"


def reviewer_grants(home: Path, skill: str) -> list[str]:
    """The commands a reviewer runs besides its file tools, granted in both shells at each spelling of the home's
    scripts: its self-check, which review_pipeline.py writes as python -B "<script>" validate-result --run "<run>"
    --role "<role>", and a lazy snapshot's two source commands, which review_source.py writes as python -B "<script>"
    source-file --run "<run>" --role "<role>" --path="<path>", and the same with source-search and --pattern. Each
    names its script by its quoted path, so no other file matches; validation holds them to the skill grant rules.
    review-insights' synthesis runs only its self-check, granted at the deployed script's resolved path."""
    grants: list[str] = []
    if skill == SYNTHESIS_SKILL:
        # The synthesis's one command, its self-check, which review_synthesis.py writes as python -B "<script>"
        # synthesize --report "<report>" --result "<result>" --check, naming the deployed script's resolved path.
        scripts = skill_scripts_folder(home, skill)
        return [
            f'Bash({PYTHON} -B "{scripts}review_insights.py" synthesize --report *)',
            f'PowerShell({PYTHON} -B "{scripts}review_insights.py" synthesize --report *)',
        ]
    for folder in reviewer_script_folders(home, skill):
        grants += [
            f'Bash({PYTHON} -B "{folder}review_pipeline.py" validate-result --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_pipeline.py" validate-result --run *)',
            f'Bash({PYTHON} -B "{folder}review_source.py" source-file --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_source.py" source-file --run *)',
            f'Bash({PYTHON} -B "{folder}review_source.py" source-search --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_source.py" source-search --run *)',
        ]
    return grants


def claude_command(executable: str, request: str, home: Path, skill: str) -> list[str]:
    # Project settings only, so the installed skills and agents do not load beside the home's; the reviewer agent from
    # the home's file for --agents, so its guard hook runs; and no Workflow tool, so reviewers start as native
    # subagents whose messages the transcript carries with their model.
    return [
        executable,
        "-p",
        request,
        "--model",
        MODELS[SESSION_MODEL],
        "--output-format",
        "stream-json",
        "--verbose",
        "--forward-subagent-text",
        "--include-hook-events",
        "--setting-sources",
        "project,local",
        "--agents",
        str(home / STATE / AGENTS),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disallowedTools",
        "Workflow",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *reviewer_grants(home, skill),
    ]


def trigger_command(executable: str, request: str, model: str) -> list[str]:
    # The request alone, as a user would type it, on the judged model, since the session's model is the one that
    # decides whether to start a skill; project settings only, so only the home's skills are listed; and a turn limit,
    # so a session that starts one stops soon after. Nothing is granted, so a command the skill would run is denied.
    return [
        executable,
        "-p",
        request,
        "--model",
        MODELS[model],
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns",
        str(TRIGGER_TURNS),
    ]


# A session's own message, or a Skill call in it, in a shape the evaluation does not read, whose starts cannot then be
# known: reading it as none would record that the session started no skill.
UNREAD_STARTS = "the session's stream holds a message or a Skill call in a shape the evaluation does not read"


class StreamShapeError(claude_stream.StreamError):
    """A session's stream whose skill starts cannot be read."""


def skills_in_stream(output: str) -> list[str]:
    """The skills the session's own messages started with the Skill tool, each once, in the order first started; a
    subagent's start is not the session's. A message of the session's whose content is not a list of blocks, or a Skill
    call whose input names no skill as text, raises StreamShapeError."""
    found: list[str] = []
    for event in claude_stream.events(output):
        if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
            raise StreamShapeError(UNREAD_STARTS)
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                tool_input = block.get("input")
                name = tool_input.get("skill") if isinstance(tool_input, dict) else None
                if not isinstance(name, str):
                    raise StreamShapeError(UNREAD_STARTS)
                if name.strip():
                    found.append(name.strip().removeprefix("/"))
    return list(dict.fromkeys(found))


def trigger_environment(base: Mapping[str, str], run: Path) -> dict[str, str]:
    """The user's environment, sign-in included, with the run's own temporary directory, and with
    CLAUDE_INSTRUCTIONS_OFF_VARIABLES, so the session reads no CLAUDE.md from a folder above the home, nor any of the
    user's own, nor auto memory, and with AUTO_MEMORY_OFF_VARIABLES, so it writes no auto memory either."""
    return {**base, "TMPDIR": str(run / "tmp"), **CLAUDE_INSTRUCTIONS_OFF_VARIABLES, **AUTO_MEMORY_OFF_VARIABLES}


def run_environment(base: Mapping[str, str], run: Path, config: Path) -> dict[str, str]:
    """The trigger_environment, with the run's own code-review files, and without the effort variable that would
    outrank the reviewer's stated effort."""
    return {
        **trigger_environment({name: value for name, value in base.items() if name not in EFFORT_VARIABLES}, run),
        "CODE_REVIEW_CONFIG": str(config),
        "CODE_REVIEW_STATE": str(run / "state.json"),
        "CODE_REVIEW_FLAGS": str(run / "flags.json"),
    }


def set_reviewer_model(home: Path, model: str) -> str | None:
    """Put the model's ID in the home's copy of the reviewer agent in place of `model: inherit`, and its reviewer
    effort on the line after, in place of any effort the agent states; return why it could not, or None."""
    path = home / REVIEWER_AGENT
    try:
        lines = LINE_BREAK.split(path.read_text(encoding="utf-8"))
        end = lines.index("---", 1)
        head = [line for line in lines[1:end] if not line.startswith("effort:")]
        at = head.index("model: inherit")
    except (OSError, ValueError):
        return f"{runtime_canary.forward(path)} has no frontmatter line `model: inherit` to set"
    head[at : at + 1] = [f"model: {MODELS[model]}", f"effort: {REVIEWER_EFFORT[model]}"]
    path.write_text("\n".join([lines[0], *head, *lines[end:]]), encoding="utf-8")
    values = frontmatter.read(path)
    if values.string("model") == MODELS[model] and values.string("effort") == REVIEWER_EFFORT[model]:
        return None
    return f"the reviewer agent does not read as {MODELS[model]} at {REVIEWER_EFFORT[model]} effort"


class AgentError(Exception):
    """The home's reviewer agent cannot be given to the session with --agents as it is."""


def _nested(lines: Sequence[str], key: str) -> str:
    """The scalar of the one `key:` among the hook lines, read by the frontmatter reader: its line and the lines
    indented below it, moved to the top level."""
    starts = [index for index, line in enumerate(lines) if line.lstrip(" -").startswith(f"{key}:")]
    if len(starts) != 1:
        raise AgentError(f"its hooks name {key} {len(starts)} times, not once")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip(" -"))
    block = [lines[start][indent:]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        block.append(line[indent:])
    try:
        value = frontmatter.parse("\n".join(["---", *block, "---"])).string(key)
    except frontmatter.FrontmatterError as exc:
        raise AgentError(f"its hook's {key} cannot be read: {exc}") from exc
    if not value:
        raise AgentError(f"its hook's {key} is empty")
    return value


def _guard_hook(lines: Sequence[str], home: Path) -> dict[str, Any]:
    """The agent's one PreToolUse command hook, running the home's guard in place of the profile folder's."""
    events = [line.strip() for line in lines if line.strip() and len(line) - len(line.lstrip(" ")) == 2]
    if events != ["PreToolUse:"] or _nested(lines, "type") != "command":
        raise AgentError("its hooks are not one PreToolUse command hook")
    command = _nested(lines, "command")
    if command.count(PROFILE_GUARD) != 1:
        raise AgentError(f"its hook does not name {PROFILE_GUARD} once")
    guard = home / GUARD
    file_presence.require(
        guard,
        AgentError,
        f"the home has no {GUARD.as_posix()} for its hook to run",
        f"the home's {GUARD.as_posix()} for its hook to run",
    )
    if UNQUOTABLE.search(runtime_canary.forward(home)):
        raise AgentError(f"its hook cannot quote the home's path {runtime_canary.forward(home)}")
    timeout = _nested(lines, "timeout")
    seconds = whole_number.read(timeout, AgentError(f"its hook's timeout {timeout} is not a whole number of seconds"))
    # A guard that cannot start or times out lets the call through unless its hook blocks on failure, so a session
    # never runs a reviewer whose hook does not.
    on_failure = _nested(lines, "onFailure")
    if on_failure != "block":
        raise AgentError(f"its hook's onFailure is {on_failure}, not block")
    hook = {"type": "command", "command": command.replace(PROFILE_GUARD, runtime_canary.forward(guard))}
    return {"matcher": _nested(lines, "matcher"), "hooks": [{**hook, "timeout": seconds, "onFailure": on_failure}]}


def reviewer_agents(home: Path) -> dict[str, Any]:
    """The --agents definition of the home's reviewer agent: its frontmatter and body, its hook running the home's
    guard. Raise AgentError when the agent has anything the definition would drop or cannot carry."""
    path = home / REVIEWER_AGENT
    try:
        lines = text_lines(path.read_text(encoding="utf-8"))
        found = frontmatter.split(lines)
        if found is None:
            raise AgentError("it has no frontmatter")
        head, body = found
        values = frontmatter.Frontmatter(head)
        extra = sorted(set(values.keys()) - AGENT_KEYS)
        if extra:
            raise AgentError(f"the evaluation does not pass {', '.join(extra)} with --agents")
        name = values.string("name")
        if name != path.stem:
            raise AgentError(f"it is named {name}, not {path.stem}")
        if "hooks:" not in head:
            raise AgentError("it has no hooks")
        after = head[head.index("hooks:") + 1 :]
        hook_lines = list(itertools.takewhile(lambda line: not line or line[0] == " ", after))
        definition: dict[str, Any] = {
            "description": values.string("description"),
            "prompt": "\n".join(lines[body:]).strip("\n"),
            "tools": [tool.strip() for tool in (values.string("tools") or "").split(",")],
            "model": values.string("model"),
            "hooks": {"PreToolUse": [_guard_hook(hook_lines, home)]},
        }
        if values.string("effort") is not None:
            definition["effort"] = values.string("effort")
        if values.string("omitClaudeMd") == "true":
            definition["omitClaudeMd"] = True
    except (OSError, ValueError) as exc:
        raise AgentError(str(exc)) from exc
    return {path.stem: definition}


def stream_models(output: str) -> tuple[frozenset[str], frozenset[str]]:
    """The models the session's own messages named, and those its subagents' messages named."""
    session: set[str] = set()
    subagents: set[str] = set()
    for event in claude_stream.events(output):
        if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
            continue
        model = event["message"].get("model")
        # A message Claude Code writes itself, such as an API error, names no model of its own.
        if isinstance(model, str) and model.startswith("claude-"):
            (subagents if event.get("parent_tool_use_id") else session).add(model)
    return frozenset(session), frozenset(subagents)


def latest_review(pull: Path, version: int) -> Record | str:
    """A pull request folder's latest record, read and validated by ejryan89-code-review-core's archive, when it is this
    review version, or else what its latest record is. The folder is <canary root>/<owner>/<name>/pulls/<number>."""
    latest = max(list_versions(pull), default=None)
    try:
        record = latest_record(pull.parents[3], f"{pull.parents[2].name}/{pull.parents[1].name}", int(pull.name))
    except (ArchiveError, PersistenceError, OSError, ValueError) as exc:
        return f"its latest record cannot be read: {exc}"
    if record is None:
        return "it holds no record"
    found = record["review"]["version"] if latest == version else latest
    return record if found == version else f"its latest record is version {found}"


def find_record(directory: Path, version: int) -> Record:
    """The record a run wrote: the latest record of the pull request under the canary roots in its temporary
    directory, which must be this review version. Raise RecordError when none is, saying what each pull request's
    latest record is instead, or when more than one is, since which is the run's own is then unknown."""
    pulls = sorted(
        path
        for path in file_presence.matching(directory, "code-review-canary-*/*/*/pulls/*")
        if file_presence.is_directory(path, other_is_absent=True)
    )
    if not pulls:
        raise RecordError("the run recorded no review")
    latest = [latest_review(pull, version) for pull in pulls]
    found = [record for record in latest if not isinstance(record, str)]
    if len(found) > 1:
        raise RecordError(f"the run recorded {len(found)} reviews at version {version}, so which is its own is unknown")
    if not found:
        reasons = "; ".join(reason for reason in latest if isinstance(reason, str))
        raise RecordError(f"the run recorded no review version {version}; {reasons}")
    return found[0]


def run_failure(
    model: str,
    completed: Completed,
    timeout: float,
    skill: str,
    missing: str | None,
    recorded: bool = False,
    pins: Sequence[RoutePin] = (),
    absent: str = NO_REVIEW,
) -> str | None:
    """Why a run counts for nothing, or None when it recorded a review on the model it was given, its reviewers on that
    model or on a model a route of the scenario's manifest pins, which the pipeline starts that route's reviewer on.
    With `recorded`, the reason as the results file records it: why the record is missing, why Claude Code could not
    start, and each denied command, which may name the machine's paths, are each a fixed phrase; `absent` is the
    phrase for a missing record, NO_REVIEW or a synthesis run's NO_SYNTHESIS."""
    problems = session_problems(completed.stdout, skill, recorded)
    session, reviewers = stream_models(completed.stdout)
    if missing is not None:
        reasons = [absent if recorded else missing]
        if completed.error:
            reasons.append("Claude Code could not start" if recorded else completed.error)
        elif completed.timed_out:
            reasons.append(f"timed out after {timeout:g} seconds")
        elif completed.returncode != 0:
            reasons.append(f"Claude Code exited with code {completed.returncode}")
        return "; ".join([*reasons, *problems])
    if not reviewers:
        return "no reviewer subagent ran, so the record is not the model's"
    pinned = [identifier for identifier in pinned_models(pins) if identifier != MODELS[model]]
    allowed = [MODELS[model], *pinned]
    other = sorted(name for name in reviewers if not any(names_model(name, identifier) for identifier in allowed))
    if other:
        routes = f" or the pinned {', '.join(pinned)}" if pinned else ""
        return f"reviewers ran on {', '.join(other)}, not {MODELS[model]}{routes}"
    elsewhere = sorted(name for name in session if not names_model(name, MODELS[SESSION_MODEL]))
    if elsewhere:
        return f"the session ran on {', '.join(elsewhere)}, not {MODELS[SESSION_MODEL]}"
    return None


def session_problems(output: str, skill: str, recorded: bool = False) -> list[str]:
    """What a session's stream shows went wrong outside the model's choices: Claude Code discovered no skill or did not
    list this one, and the commands it denied, each named, or with `recorded` counted, as the results file records
    them. A stream in a shape this does not read raises StreamError."""
    listed, denials = runtime_canary.claude_stream(output)
    problems = []
    if nothing := runtime_canary.discovery_failure(output):
        problems.append(nothing)
    elif listed is not None and skill not in listed:
        problems.append(f"Claude Code did not list {skill}")
    if denials and recorded:
        problems.append(f"denied {len(denials)} command{'' if len(denials) == 1 else 's'}")
    elif denials:
        problems.append(f"denied: {'; '.join(denials)}")
    return problems


def _content(event: Mapping[str, Any]) -> list[Any]:
    """A user event's message content blocks, or none when its content is text, such as the prompt."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def ended_without_error(output: str) -> bool:
    """Whether the stream's last result event says the session ended with no error. A session that was ended at its
    time limit, or by anything else, wrote none."""
    results = [event for event in claude_stream.events(output) if event.get("type") == "result"]
    return bool(results) and results[-1].get("is_error") is False


def unrecorded_calls(output: str, scenario: Scenario) -> list[dict[str, Any]] | None:
    """The session's own tool calls in the stream of a by-name run that recorded no review, which the `resolved` kind
    still judges, when the stream shows the session's own choices on its model: session_problems finds nothing, the
    last result event says the session ended with no error, every message of the session's names SESSION_MODEL's ID,
    as a run that counts must; and of those calls, the ones judged_calls keeps after a last `resolve` that ended in an
    error. None otherwise. A stream in a shape this does not read raises StreamError."""
    session, _ = stream_models(output)
    if (
        session_problems(output, scenario.skill)
        or not ended_without_error(output)
        or not session
        or not all(names_model(name, MODELS[SESSION_MODEL]) for name in session)
    ):
        return None
    name = scenario.document.name if scenario.document is not None else ""
    judged = judged_calls(session_tool_calls(output), name)
    return None if judged is None else [dict(call) for call in judged]


@dataclass(frozen=True)
class Session:
    """What every run starts its session with: Claude Code, the user's environment without git's repository-selecting
    variables, the runner, its time limit, and the git client a document scenario's checkout is made with."""

    executable: str
    base: Mapping[str, str]
    runner: Runner
    timeout: float
    git: GitClient


def run_folder(home: Path, scenario: str, index: Index) -> Path:
    """A run's own folder in the model's home: the scenario's, under the run's number when it runs several times."""
    return home / STATE / scenario if index is None else home / STATE / str(index) / scenario


def run_scenario(
    scenario: Scenario, model: str, home: Path, session: Session, index: Index = None
) -> tuple[Run, list[Path]]:
    """Run the scenario once in the model's home; return what it gave and its transcripts, one per session."""
    run = run_folder(home, scenario.name, index)
    (run / "tmp").mkdir(parents=True)
    if scenario.mode == TRIGGER:
        return run_trigger(scenario, model, home, session, run)
    result, transcript = review_run(scenario, model, home, session, run)
    return result, [transcript]


def trigger_failure(model: str, completed: Completed, timeout: float, skill: str) -> tuple[str, str, str] | None:
    """Why a trigger session counts for nothing, as the FAIL lines and as the results file record it, and its kind;
    or None. Its exit code is not read, since the turn limit that stops a session after it starts a skill is the
    evaluation's own. A stream in a shape this does not read raises StreamError."""
    if completed.error:
        return completed.error, "Claude Code could not start", RUN
    if completed.timed_out:
        return f"timed out after {timeout:g} seconds", f"timed out after {timeout:g} seconds", RUN
    listed, _ = runtime_canary.claude_stream(completed.stdout)
    if nothing := runtime_canary.discovery_failure(completed.stdout):
        return nothing, nothing, RUN
    if listed is None or skill not in listed:
        return f"Claude Code did not list {skill}", f"Claude Code did not list {skill}", RUN
    session, _ = stream_models(completed.stdout)
    other = sorted(name for name in session if not names_model(name, MODELS[model]))
    if not session or other:
        reason = f"the session ran on {', '.join(other) or 'no model it named'}, not {MODELS[model]}"
        return reason, reason, RECORD
    return None


def run_trigger(scenario: Scenario, model: str, home: Path, session: Session, run: Path) -> tuple[Run, list[Path]]:
    """Give each of a trigger scenario's requests, then each near miss, to a session of its own in the model's home,
    keep each transcript, and write the trigger record of the skills each session started in the run's folder. The
    first session that counts for nothing makes the run count for nothing."""
    environment = trigger_environment(session.base, run)
    entries: list[dict[str, Any]] = []
    models: set[str] = set()
    transcripts: list[Path] = []
    failure: tuple[str, str, str] | None = None
    for number, request in enumerate([*scenario.requests, *scenario.near_misses], 1):
        completed = session.runner(
            trigger_command(session.executable, request, model), home, environment, session.timeout
        )
        transcript = run / f"request-{number}.jsonl"
        transcript.write_text(completed.stdout, encoding="utf-8")
        if completed.stderr:
            transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
        transcripts.append(transcript)
        unread: tuple[str, str, str] | None = None
        try:
            models |= stream_models(completed.stdout)[0]
            started = skills_in_stream(completed.stdout)
        except claude_stream.StreamError as exc:
            started, unread = [], (str(exc), str(exc), RUN)
        entries.append({"request": request, "started": started})
        try:
            problem = trigger_failure(model, completed, session.timeout, scenario.skill) or unread
        except claude_stream.StreamError:
            problem = unread
        if failure is None and problem is not None:
            failure = (f"request {number}: {problem[0]}", f"request {number}: {problem[1]}", problem[2])
    record = {"requests": entries, "models": sorted(models)}
    json_writer.write(run / TRIGGER_RECORD, record, indent=2)
    judged = frozenset(models)
    if failure is not None:
        return Run(None, failure[0], judged, judged, failure[1], failure[2]), transcripts
    return Run(record, None, judged, judged), transcripts


def review_run(scenario: Scenario, model: str, home: Path, session: Session, run: Path) -> tuple[Run, Path]:
    """Run a review or synthesis scenario once in its run's folder; return what it gave and its transcript."""
    transcript = run / "transcript.jsonl"
    if scenario.mode == SYNTHESIS:
        try:
            arguments = stage_synthesis(scenario, run)
        except OSError as exc:
            transcript.write_text("", encoding="utf-8")
            failure = "the synthesis fixture could not be staged"
            return Run(None, f"{failure}: {exc}", frozenset(), frozenset(), failure, RUN), transcript
        return _session_run(scenario, model, home, session, arguments, transcript)
    with contextlib.ExitStack() as copies:
        try:
            if scenario.by_name:
                arguments = copies.enter_context(named_document(scenario, home, session.git))
            else:
                fixture = copies.enter_context(reviewed_fixture(scenario))
                place = copies.enter_context(document_place(scenario, run))
                arguments = skill_arguments(scenario, place, session.git, fixture)
        except (GitError, OSError) as exc:
            transcript.write_text("", encoding="utf-8")
            staged = "fixture" if scenario.document is None else "document"
            failure = f"the {staged} could not be staged"
            return Run(None, f"{failure}: {exc}", frozenset(), frozenset(), failure, RUN), transcript
        return _session_run(scenario, model, home, session, arguments, transcript)


def _session_run(
    scenario: Scenario, model: str, home: Path, session: Session, arguments: Sequence[str], transcript: Path
) -> tuple[Run, Path]:
    """Start the session on the skill with its arguments in the model's home, keep its transcript, and find the
    record the run wrote."""
    run = transcript.parent
    synthesis = scenario.mode == SYNTHESIS
    config = run / SYNTHESIS_CONFIG if synthesis else home / STATE / "config.json"
    environment = run_environment(session.base, run, config)
    command = claude_command(session.executable, prompt(scenario.skill, arguments), home, scenario.skill)
    completed = session.runner(command, home, environment, session.timeout)
    transcript.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
    record: Record | None = None
    missing = None
    try:
        record = find_synthesis(run, scenario) if synthesis else find_record(run / "tmp", RECORD_VERSION[scenario.mode])
    except RecordError as exc:
        missing = str(exc)
    try:
        absent = NO_SYNTHESIS if synthesis else NO_REVIEW
        failure = run_failure(model, completed, session.timeout, scenario.skill, missing, pins=scenario.pins)
        recorded = run_failure(model, completed, session.timeout, scenario.skill, missing, True, scenario.pins, absent)
        orchestrators, reviewers = stream_models(completed.stdout)
        calls = session_tool_calls(completed.stdout) if scenario.by_name else None
        # A session that ran as a counted run's must but recorded no review is still judged on where it looked.
        ran = not (completed.error or completed.timed_out or completed.returncode)
        unrecorded = (
            unrecorded_calls(completed.stdout, scenario) if calls is not None and missing is not None and ran else None
        )
    except claude_stream.StreamError as exc:
        # A stream in a shape this does not read shows neither the models nor the skills, so the run shows nothing.
        if missing is None:
            return Run(None, str(exc), frozenset(), frozenset(), str(exc), RECORD), transcript
        unread, written = f"{missing}; {exc}", f"{NO_SYNTHESIS if synthesis else NO_REVIEW}; {exc}"
        return Run(None, unread, frozenset(), frozenset(), written, RUN), transcript
    if failure is None and record is not None:
        failure = recorded = synthesis_unguarded(completed.stdout) if synthesis else unguarded(record)
    if record is not None and calls is not None:
        record = {**record, SESSION_CALLS: calls}
    kind = None if failure is None else RUN if missing is not None else RECORD
    return Run(None if failure else record, failure, reviewers, orchestrators, recorded, kind, unrecorded), transcript


def run_keys(scenarios: Sequence[Scenario], models: Sequence[str], runs: Mapping[RunKey, Run]) -> list[RunKey]:
    """The key of each finished run, by scenario, then model, then run, whatever order the runs ended in."""
    numbers = sorted({key[2] for key in runs}, key=lambda index: index or 0)
    return [
        (scenario.name, model, index)
        for scenario in scenarios
        for model in models
        for index in numbers
        if (scenario.name, model, index) in runs
    ]


def run_lines(scenarios: Sequence[Scenario], models: Sequence[str], runs: Mapping[RunKey, Run]) -> list[str]:
    """The REVIEWER line of each model, then the GUARDED lines of each run."""
    keys = run_keys(scenarios, models, runs)
    lines = []
    for model in models:
        used = sorted({name for key in keys if key[1] == model for name in runs[key].reviewers})
        lines.append(f"REVIEWER {model} {','.join(used) or 'none'}")
    for key in keys:
        lines += guarded_lines(*key, runs[key])
    return lines


def guarded_lines(scenario: str, model: str, index: Index, run: Run) -> list[str]:
    """One line per reviewer of a run that counts, which `unguarded` found the guard held, with its count. A trigger
    scenario's record has no reviewers."""
    if run.record is None or "review" not in run.record:
        return []
    return [
        f"GUARDED {scenario} {model}{run_token(index)} {reviewer['id']} files_read={reviewer['files_read']}"
        for reviewer in review(run.record)["reviewers"]
    ]


def prepare_home(home: Path, model: str, deploy: Callable[[Path, Path], tuple[int, str]]) -> str | None:
    """Deploy the checkout into the model's home, make the home a git repository of its own, as the runtime canary
    makes its home, set its reviewer model, and write the reviewer definition the session gets with --agents; return
    why that failed, or None."""
    if "*" in str(home):
        return (
            f"the home's path {runtime_canary.forward(home)} holds a *, which a reviewer's grant would read as any text"
        )
    home.mkdir(parents=True)
    code, log = deploy(home, REPOSITORY_ROOT)
    if code != 0:
        (home / STATE).mkdir(exist_ok=True)
        log_file = home / STATE / "deploy.log"
        log_file.write_text(log, encoding="utf-8")
        return f"the deployment failed; its log is {runtime_canary.forward(log_file)}"
    reason = runtime_canary.bound_home(home) or set_reviewer_model(home, model)
    if reason:
        return reason
    try:
        agents = reviewer_agents(home)
    except AgentError as exc:
        return f"{runtime_canary.forward(home / REVIEWER_AGENT)} cannot be passed with --agents: {exc}"
    (home / STATE).mkdir(exist_ok=True)
    json_writer.write(home / STATE / AGENTS, agents, indent=2)
    json_writer.write(home / STATE / "config.json", review_config(home / STATE), indent=2)
    return None


def run_all(
    scenarios: Sequence[Scenario], homes: Mapping[str, Path], session: Session, jobs: int, runs: int = 1
) -> dict[RunKey, Run]:
    """Every scenario on every model with a home, `runs` times, up to `jobs` at once, printing each transcript as its
    run ends. Each round of every scenario on every model is queued before the next. One job runs in this thread, in
    order. The by-name runs on one model, which stage their document in that model's home, run one at a time as one
    job, queued where the first of them is, so no run's `resolve` finds another's copy; each prints its transcript as
    the job ends."""
    results: dict[RunKey, Run] = {}
    queued = [
        (scenario, model, home, index)
        for index in indexes(runs)
        for scenario in scenarios
        for model, home in homes.items()
    ]
    chains: dict[str, list[Queued]] = {}
    batches: list[list[Queued]] = []
    for item in queued:
        if not item[0].by_name:
            batches.append([item])
        elif item[1] in chains:
            chains[item[1]].append(item)
        else:
            batches.append(chains.setdefault(item[1], [item]))

    def ended(scenario: Scenario, model: str, index: Index, result: tuple[Run, list[Path]]) -> None:
        results[(scenario.name, model, index)] = result[0]
        for transcript in result[1]:
            path = quote(runtime_canary.forward(transcript))
            print(f"TRANSCRIPT {scenario.name} {model}{run_token(index)} {path}", flush=True)

    def run_batch(batch: Sequence[Queued]) -> list[tuple[Queued, tuple[Run, list[Path]]]]:
        return [(item, run_scenario(item[0], item[1], item[2], session, item[3])) for item in batch]

    if jobs == 1:
        for scenario, model, home, index in queued:
            ended(scenario, model, index, run_scenario(scenario, model, home, session, index))
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = [pool.submit(run_batch, batch) for batch in batches]
        for future in concurrent.futures.as_completed(pending):
            for item, result in future.result():
                ended(item[0], item[1], item[3], result)
    return results


def runs_source(runs: Mapping[RunKey, Run]) -> RecordSource:
    """A record source over finished runs."""

    def source(scenario: Scenario, model: str, index: Index) -> Record:
        run = runs[(scenario.name, model, index)]
        if run.record is None:
            raise RecordError(run.failure or NO_REVIEW, run.kind or RUN, run.calls)
        return run.record

    return source


def claude_version(executable: str, runner: Runner, base: Mapping[str, str], directory: Path) -> str:
    completed = runner([executable, "--version"], directory, dict(base), 60)
    version = tools.parse_version(completed.stdout) if completed.returncode == 0 else None
    return tools.format_version(version) if version else "unknown"

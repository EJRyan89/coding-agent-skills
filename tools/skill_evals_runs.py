"""Running a skill's evaluation scenarios: each model's throwaway home, the Claude Code session, and what each run
shows of the models it used."""

from __future__ import annotations

import concurrent.futures
import contextlib
import itertools
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import frontmatter
from git_client import GitClient, GitError
from review_archive import ArchiveError, latest_record, list_versions
from review_io import PersistenceError

from deployer import tools
from deployer.platform_support import PYTHON
from tools import runtime_canary
from tools.runtime_canary import Completed, Runner
from tools.skill_evals_records import MODELS, RECORD, RUN, Index, Record, RecordError, names_model, review, unguarded
from tools.skill_evals_scenarios import REPOSITORY_ROOT, TRIGGER, Scenario, quote, reviewed_fixture, skill_arguments

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
AGENT_KEYS = frozenset({"name", "description", "tools", "model", "omitClaudeMd", "hooks"})
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
RunKey = tuple[str, str, Index]
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


def reviewer_grants(home: Path, skill: str) -> list[str]:
    """The commands a reviewer runs besides its file tools, granted in both shells at each spelling of the home's
    scripts: its self-check, which review_pipeline.py writes as python -B "<script>" validate-result --run "<run>"
    --role "<role>", and a lazy snapshot's two source commands, which review_source.py writes as python -B "<script>"
    source-file --run "<run>" --role "<role>" --path="<path>", and the same with source-search and --pattern. Each
    names its script by its quoted path, so no other file matches; validation holds them to the skill grant rules."""
    grants: list[str] = []
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


def skills_in_stream(output: str) -> list[str]:
    """The skills the session's own messages started with the Skill tool, each once, in the order first started; a
    subagent's start is not the session's."""
    found: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                name = block.get("input", {}).get("skill") if isinstance(block.get("input"), dict) else None
                if isinstance(name, str) and name.strip():
                    found.append(name.strip().removeprefix("/"))
    return list(dict.fromkeys(found))


def run_environment(base: Mapping[str, str], run: Path, config: Path) -> dict[str, str]:
    """The user's environment, sign-in included, with the run's own temporary directory and code-review files."""
    return {
        **base,
        "TMPDIR": str(run / "tmp"),
        "CODE_REVIEW_CONFIG": str(config),
        "CODE_REVIEW_STATE": str(run / "state.json"),
        "CODE_REVIEW_FLAGS": str(run / "flags.json"),
    }


def set_reviewer_model(home: Path, model: str) -> str | None:
    """Put the model's ID in the home's copy of the reviewer agent in place of `model: inherit`; return why it
    could not, or None."""
    path = home / REVIEWER_AGENT
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        at = lines.index("model: inherit", 1, lines.index("---", 1))
    except (OSError, ValueError):
        return f"{runtime_canary.forward(path)} has no frontmatter line `model: inherit` to set"
    lines[at] = f"model: {MODELS[model]}"
    path.write_text("\n".join(lines), encoding="utf-8")
    if frontmatter.read(path).string("model") == MODELS[model]:
        return None
    return f"the reviewer agent does not read as {MODELS[model]}"


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
    if not guard.is_file():
        raise AgentError(f"the home has no {GUARD.as_posix()} for its hook to run")
    if UNQUOTABLE.search(runtime_canary.forward(home)):
        raise AgentError(f"its hook cannot quote the home's path {runtime_canary.forward(home)}")
    timeout = _nested(lines, "timeout")
    if not timeout.isdigit():
        raise AgentError(f"its hook's timeout {timeout} is not a whole number of seconds")
    # A guard that cannot start or times out lets the call through unless its hook blocks on failure, so a session
    # never runs a reviewer whose hook does not.
    on_failure = _nested(lines, "onFailure")
    if on_failure != "block":
        raise AgentError(f"its hook's onFailure is {on_failure}, not block")
    hook = {"type": "command", "command": command.replace(PROFILE_GUARD, runtime_canary.forward(guard))}
    return {"matcher": _nested(lines, "matcher"), "hooks": [{**hook, "timeout": int(timeout), "onFailure": on_failure}]}


def reviewer_agents(home: Path) -> dict[str, Any]:
    """The --agents definition of the home's reviewer agent: its frontmatter and body, its hook running the home's
    guard. Raise AgentError when the agent has anything the definition would drop or cannot carry."""
    path = home / REVIEWER_AGENT
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
        if values.string("omitClaudeMd") == "true":
            definition["omitClaudeMd"] = True
    except (OSError, ValueError) as exc:
        raise AgentError(str(exc)) from exc
    return {path.stem: definition}


def stream_models(output: str) -> tuple[frozenset[str], frozenset[str]]:
    """The models the session's own messages named, and those its subagents' messages named."""
    session: set[str] = set()
    subagents: set[str] = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(event, dict)
            or event.get("type") != "assistant"
            or not isinstance(event.get("message"), dict)
        ):
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
    pulls = sorted(path for path in directory.glob("code-review-canary-*/*/*/pulls/*") if path.is_dir())
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
    model: str, completed: Completed, timeout: float, skill: str, missing: str | None, recorded: bool = False
) -> str | None:
    """Why a run counts for nothing, or None when it recorded a review on the model it was given. With `recorded`, the
    reason as the results file records it: why the record is missing, why Claude Code could not start, and each
    denied command, which may name the machine's paths, are each a fixed phrase."""
    listed, denials = runtime_canary.claude_stream(completed.stdout)
    session, reviewers = stream_models(completed.stdout)
    if missing is not None:
        reasons = [NO_REVIEW if recorded else missing]
        if completed.error:
            reasons.append("Claude Code could not start" if recorded else completed.error)
        elif completed.timed_out:
            reasons.append(f"timed out after {timeout:g} seconds")
        elif completed.returncode != 0:
            reasons.append(f"Claude Code exited with code {completed.returncode}")
        if listed is not None and skill not in listed:
            reasons.append(f"Claude Code did not list {skill}")
        if denials and recorded:
            reasons.append(f"denied {len(denials)} command{'' if len(denials) == 1 else 's'}")
        elif denials:
            reasons.append(f"denied: {'; '.join(denials)}")
        return "; ".join(reasons)
    if not reviewers:
        return "no reviewer subagent ran, so the record is not the model's"
    other = sorted(name for name in reviewers if not names_model(name, MODELS[model]))
    if other:
        return f"reviewers ran on {', '.join(other)}, not {MODELS[model]}"
    elsewhere = sorted(name for name in session if not names_model(name, MODELS[SESSION_MODEL]))
    if elsewhere:
        return f"the session ran on {', '.join(elsewhere)}, not {MODELS[SESSION_MODEL]}"
    return None


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
    evaluation's own."""
    listed, _ = runtime_canary.claude_stream(completed.stdout)
    if completed.error:
        return completed.error, "Claude Code could not start", RUN
    if completed.timed_out:
        return f"timed out after {timeout:g} seconds", f"timed out after {timeout:g} seconds", RUN
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
    environment = {**session.base, "TMPDIR": str(run / "tmp")}
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
        models |= stream_models(completed.stdout)[0]
        entries.append({"request": request, "started": skills_in_stream(completed.stdout)})
        problem = trigger_failure(model, completed, session.timeout, scenario.skill)
        if failure is None and problem is not None:
            failure = (f"request {number}: {problem[0]}", f"request {number}: {problem[1]}", problem[2])
    record = {"requests": entries, "models": sorted(models)}
    (run / TRIGGER_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")
    judged = frozenset(models)
    if failure is not None:
        return Run(None, failure[0], judged, judged, failure[1], failure[2]), transcripts
    return Run(record, None, judged, judged), transcripts


def review_run(scenario: Scenario, model: str, home: Path, session: Session, run: Path) -> tuple[Run, Path]:
    """Run a review scenario once in its run's folder; return what it gave and its transcript."""
    transcript = run / "transcript.jsonl"
    with contextlib.ExitStack() as copies:
        try:
            fixture = copies.enter_context(reviewed_fixture(scenario))
            arguments = skill_arguments(scenario, run, session.git, fixture)
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
    environment = run_environment(session.base, run, home / STATE / "config.json")
    command = claude_command(session.executable, prompt(scenario.skill, arguments), home, scenario.skill)
    completed = session.runner(command, home, environment, session.timeout)
    transcript.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
    record: Record | None = None
    missing = None
    try:
        record = find_record(run / "tmp", RECORD_VERSION[scenario.mode])
    except RecordError as exc:
        missing = str(exc)
    failure = run_failure(model, completed, session.timeout, scenario.skill, missing)
    recorded = run_failure(model, completed, session.timeout, scenario.skill, missing, recorded=True)
    if failure is None and record is not None:
        failure = recorded = unguarded(record)
    orchestrators, reviewers = stream_models(completed.stdout)
    kind = None if failure is None else RUN if missing is not None else RECORD
    return Run(None if failure else record, failure, reviewers, orchestrators, recorded, kind), transcript


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
    """Deploy the checkout into the model's home, set its reviewer model, and write the reviewer definition the
    session gets with --agents; return why that failed, or None."""
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
    reason = set_reviewer_model(home, model)
    if reason:
        return reason
    try:
        agents = reviewer_agents(home)
    except AgentError as exc:
        return f"{runtime_canary.forward(home / REVIEWER_AGENT)} cannot be passed with --agents: {exc}"
    (home / STATE).mkdir(exist_ok=True)
    (home / STATE / AGENTS).write_text(json.dumps(agents, indent=2), encoding="utf-8")
    (home / STATE / "config.json").write_text(json.dumps(review_config(home / STATE), indent=2), encoding="utf-8")
    return None


def run_all(
    scenarios: Sequence[Scenario], homes: Mapping[str, Path], session: Session, jobs: int, runs: int = 1
) -> dict[RunKey, Run]:
    """Every scenario on every model with a home, `runs` times, up to `jobs` at once, printing each transcript as its
    run ends. Each round of every scenario on every model is queued before the next. One job runs in this thread, in
    order."""
    results: dict[RunKey, Run] = {}
    queued = [
        (scenario, model, home, index)
        for index in indexes(runs)
        for scenario in scenarios
        for model, home in homes.items()
    ]

    def ended(scenario: Scenario, model: str, index: Index, result: tuple[Run, list[Path]]) -> None:
        results[(scenario.name, model, index)] = result[0]
        for transcript in result[1]:
            path = quote(runtime_canary.forward(transcript))
            print(f"TRANSCRIPT {scenario.name} {model}{run_token(index)} {path}", flush=True)

    if jobs == 1:
        for scenario, model, home, index in queued:
            ended(scenario, model, index, run_scenario(scenario, model, home, session, index))
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {
            pool.submit(run_scenario, scenario, model, home, session, index): (scenario, model, index)
            for scenario, model, home, index in queued
        }
        for future in concurrent.futures.as_completed(pending):
            ended(*pending[future], future.result())
    return results


def runs_source(runs: Mapping[RunKey, Run]) -> RecordSource:
    """A record source over finished runs."""

    def source(scenario: Scenario, model: str, index: Index) -> Record:
        run = runs[(scenario.name, model, index)]
        if run.record is None:
            raise RecordError(run.failure or NO_REVIEW, run.kind or RUN)
        return run.record

    return source


def claude_version(executable: str, runner: Runner, base: Mapping[str, str], directory: Path) -> str:
    completed = runner([executable, "--version"], directory, dict(base), 60)
    version = tools.parse_version(completed.stdout) if completed.returncode == 0 else None
    return tools.format_version(version) if version else "unknown"

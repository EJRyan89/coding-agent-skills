"""PreToolUse hook for the code-review-reviewer subagent: keep a reviewer inside its own role of its review run.

A reviewer's prompt told it never to read the local checkout, and reviewers did anyway: they searched the live
working copy and the whole workspace, which may hold another branch's code. This hook enforces the boundary the
prompt describes. It reads the hook event as JSON on stdin and prints a deny decision for any call outside it:

- Every path a call names is held, by its text alone and before anything resolves it, to a local absolute path under
  the temporary directory, where prepare makes every run, or this skill's folder. A network or device path, or any
  other path, is denied unresolved, because resolving a path to a host that does not answer can block past the hook's
  timeout, and Claude Code lets a call through when its hook times out.
- First, one Read of a role's prompt file, which binds the reviewer to that role and its run: its claim. The task
  names the prompt, and nothing else may be read before it, so no content from the pull request can choose the
  role. The run is the outermost `code-review-run-*` folder holding `run.json` under the temporary directory, the
  one prepare made; a snapshot can hold a folder of that shape, so a run folder inside another run's tree is denied.
  Every other call is denied until then.
- Read, Grep, and Glob only under the claimed review run folder (a `code-review-run-*` directory holding
  `run.json`: the snapshot, the trusted reviewer files, and the work files) or this skill's `references` folder.
  Nested source folders cannot redefine the claimed root. Grep and Glob must name their folder explicitly, because
  their default is the session's working directory.
- Write and Edit only on the claimed role's result file, as the run's `run.json` names it.
- Bash only for the claimed role's self-check command, exactly as the pipeline writes it, and, for a lazy snapshot,
  its `source-file` and `source-search` commands (review_source.py), with the one path or pattern the reviewer
  fills in, which may hold no double quote (ASCII or PowerShell's smart variants), backtick, dollar sign, backslash,
  or line break. No quoted script or run in either may end in a backslash, which would escape its closing quote.
  The guarded reviewer exposes Bash, not PowerShell; its values conservatively exclude delimiters from either shell.
- A role whose `run.json` entry declares its `self_check`, as the review-insights synthesis run does, runs that one
  command, exactly as declared, and none of the reviewer commands above. The run's author is the script that made
  the run, which the guard already trusts to name the one file the role may write.

Parallel reviewers share one session, so the hook tells them apart by the `agent_id` Claude Code puts in the hook
event of a subagent's tool call, and keeps each agent's claim in a file of its own under `CLAIMS`, which no
reviewer can write. A retry is a fresh agent that claims the same role.

The hook also counts what a reviewer reads: a claim creates the role's read log in the run's `work` folder, and each
allowed Read of a file, or Grep of one file, under the run's source snapshot appends that file's path, relative to
the snapshot, as one JSON string per line, as `source-file` does for each file it fetches or finds. Only the
pipeline's `check` reads the log, and it keeps nothing of it but counts. A log that cannot be written is left
short: counting never denies a call. The log is also the claim's proof: on Claude Code, `check` refuses the result of
a role the guard never claimed (`UNGUARDED`), since no hook held its reviewer.

Other tools pass. Anything the hook cannot evaluate is denied, a call without an agent ID included: a reviewer
reading the wrong code, or writing another role's result, produces a plausible but wrong review, which is worse
than a failed one that check reports and retries.

Installed as a Claude Code plugin, the agent cannot carry this hook: a plugin agent's own hooks are ignored, so the
plugin's hooks file runs it for every matching tool call of every session, and the guard keeps to the plugin's
reviewer itself. A call with no agent ID comes from the main session, and a call of any other agent type is another
agent's; both pass. A subagent's call that carries no agent type cannot be placed, so it is denied, and a call of
the plugin's reviewer, whose type Claude Code namespaces as `<plugin>:code-review-reviewer`, is held as above.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from console import run_main
from executable_locator import PYTHON
from skill_roots import plugin_name
from typed_fields import FieldError, FieldReader

EXIT_CONTRACT_EXEMPT = "Claude Code PreToolUse hook protocol: prints a JSON decision and always exits 0"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REFERENCES = SCRIPT_DIRECTORY.parent / "references"
PIPELINE = SCRIPT_DIRECTORY / "review_pipeline.py"
RUN_PREFIX = "code-review-run-"
RUN_FILE = "run.json"
SOURCE = "source"  # the run's source snapshot
READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Write", "Edit"}
# A quoted script or run in any allowed command. No quoted value may end in a backslash, which would escape its closing
# quote and leave what follows outside quotes, though the path still normalizes to the known script or run.
SHELL_BREAKS = r'"`$\r\n\u201c\u201d\u201e'
QUOTED_VALUE = rf"[^{SHELL_BREAKS}]*[^{SHELL_BREAKS}\\]"
# Each command names Python as review-prs's rendered grant does, by the platform's command.
SELF_CHECK = re.compile(
    rf'^{re.escape(PYTHON)} -B "(?P<script>{QUOTED_VALUE})" validate-result --run "(?P<run>{QUOTED_VALUE})" '
    rf'--role "(?P<role>[a-z0-9][a-z0-9-]*)"$'
)
# The two commands review_source.py gives a lazy snapshot's reviewer. The script and the run must be the known ones;
# the path or pattern, the one value the reviewer chooses, holds no backslash at all, so it can never end up outside
# its quotes.
SOURCE_COMMAND = re.compile(
    rf'^{re.escape(PYTHON)} -B "(?P<script>{QUOTED_VALUE})" (?P<command>source-file|source-search) '
    rf'--run "(?P<run>{QUOTED_VALUE})" --role "(?P<role>[a-z0-9][a-z0-9-]*)" '
    rf'--(?P<option>path|pattern)="[^{SHELL_BREAKS}\\]+"$'
)
SOURCE_OPTIONS = {"source-file": "path", "source-search": "pattern"}
SOURCE_SCRIPT = SCRIPT_DIRECTORY / "review_source.py"
SHELL_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
# A normalized path that starts with two separators names a host or a device: `\\host\share`, `//host/share`, `\\?\`,
# and `\\.\` on Windows, and `//host` where POSIX leaves its meaning to the system.
NETWORK_OR_DEVICE = re.compile(r"^[\\/]{2}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CLAIMS = Path(tempfile.gettempdir()) / "code-review-reviewer-claims"
# The subagent every reviewer role runs as, deployed from agents/ with ejryan89-code-review-core, and the plugin these
# scripts run from, if any, which namespaces it.
REVIEWER = "code-review-reviewer"
PLUGIN = plugin_name()
FIRST = "read the prompt file your task names first"
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

Claim = tuple[Path, dict[str, str]]


def _spellings(root: Path) -> tuple[Path, ...]:
    """A trusted local folder as it is given and as it resolves, each in the case the file system compares, so a path
    in either spelling is held to it by its text."""
    return tuple(Path(os.path.normcase(os.path.normpath(form))) for form in (root.absolute(), root.resolve()))


# The folders every path the guard accepts lies under: the temporary directory, where prepare makes every run, and
# this skill's folder, which holds the references and the scripts the allowed commands run.
TEMPORARY_ROOTS = _spellings(Path(tempfile.gettempdir()))
LOCAL_ROOTS = (*TEMPORARY_ROOTS, *_spellings(Path(__file__).absolute().parents[1]))


class Denied(Exception):
    pass


class DeniedField(FieldError, Denied):
    """A hook event, run, or claim field of the wrong kind; the call is denied."""


READ = FieldReader(DeniedField)


def read_log(run: Path, role: str) -> Path:
    """The file the guard logs a role's snapshot reads in. `check` in review_pipeline.py reduces it to counts."""
    return run / "work" / f"reads-{role}.log"


def claimed(run: Path, role: str) -> bool:
    """Whether the guard claimed this role of the run and its log is still there to show it: the claim creates the
    log, and `check` deletes it once it has counted it."""
    return read_log(run, role).is_file()


# Why a result is refused on Claude Code when the guard never claimed its role, ending with the remedy, which names how
# the skill runs the role without the agent.
UNGUARDED = (
    "the reviewer ran without its guard: the reviewer guard never claimed this role, as when hooks are off "
    "(disableAllHooks, or allowManagedHooksOnly for a plugin's hooks), the code-review-reviewer agent or its hook did "
    "not load, or another agent type ran the role; enable hooks for the code-review-reviewer agent, or {remedy}"
)


def _start_log(run: Path, role: str) -> None:
    """Create the role's read log, so a reviewer the guard held is told from one no guard held, which has none."""
    if ROLE_ID.fullmatch(role):
        with contextlib.suppress(OSError):
            read_log(run, role).parent.mkdir(exist_ok=True)
            read_log(run, role).touch()


def _log_read(tool: str, path: Path, claim: Claim) -> None:
    """Append a file a reviewer read under its run's snapshot to its role's read log. A search of a folder or a
    pattern is not a read of a file, so only a Read, or a Grep naming one file, counts."""
    run, role = claim[0], claim[1]["id"]
    source = run / SOURCE
    if tool == "Glob" or not ROLE_ID.fullmatch(role) or not _inside(path, source) or not path.is_file():
        return
    root = Path(os.path.normcase(str(source.resolve(strict=False))))
    relative = Path(os.path.normcase(str(path))).relative_to(root)
    with contextlib.suppress(OSError), read_log(run, role).open("a", encoding="utf-8") as log:
        log.write(json.dumps(relative.as_posix()) + "\n")


def _path(value: Any, cwd: Path) -> Path:
    try:
        value = READ.string(value, "the path")
    except FieldError as exc:
        raise Denied("give an absolute path inside the review run") from exc
    # Git Bash names a drive as /c/, which only Windows means; elsewhere /c/ is a directory of its own.
    match = SHELL_DRIVE.match(value) if sys.platform == "win32" else None
    text = f"{match.group(1)}:/{value[match.end() :]}" if match else value
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    return _resolve_local(Path(os.path.normpath(path)))


def _resolve_local(path: Path) -> Path:
    """Resolve a normalized path only once its text shows it local and under `LOCAL_ROOTS`. Resolving a network path
    to a host that does not answer blocks for longer than the hook's timeout, and Claude Code lets a call through when
    its hook times out, so such a path is denied by its text alone."""
    if NETWORK_OR_DEVICE.match(str(path)):
        raise Denied(f"give a local path, not a network or device path; {path} is one")
    if not any(_lexically_inside(path, root) for root in LOCAL_ROOTS):
        raise Denied(f"give an absolute path inside your review run folder; {path} is outside every folder it may use")
    return Path(os.path.normpath(path.resolve(strict=False)))


def _lexically_inside(path: Path, root: Path) -> bool:
    """Whether a normalized path lies in a root `_spellings` gave, by text alone."""
    return Path(os.path.normcase(str(path))).is_relative_to(root)


def _same(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _inside(path: Path, root: Path) -> bool:
    root = Path(os.path.normcase(str(root.resolve(strict=False))))
    return Path(os.path.normcase(str(path))).is_relative_to(root)


def run_root(path: Path) -> Path | None:
    """Discover a run for a first prompt read or an explicit command root, never for a claimed source read: the
    outermost `code-review-run-*` folder holding `run.json` under the temporary directory, which prepare made. A pull
    request's snapshot can hold a folder of that shape, so one inside another run's tree is denied, never a run."""
    runs = [
        candidate
        for candidate in (path, *path.parents)
        if candidate.name.startswith(RUN_PREFIX)
        and any(_lexically_inside(candidate, root) for root in TEMPORARY_ROOTS)
        and (candidate / RUN_FILE).is_file()
    ]
    if len(runs) > 1:
        raise Denied(f"{runs[0]} sits inside the review run {runs[-1]}, so it is not a review run")
    return runs[-1] if runs else None


def _roles(run: Path) -> list[dict[str, str]]:
    """The roles a prepared run's `run.json` names, each with its ID, prompt file, and result file."""
    try:
        state = json.loads((run / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Denied(f"the review run {run} could not be read") from exc
    try:
        return READ.array(READ.json_object(state, "run").get("roles"), "run.roles", _role)
    except FieldError as exc:
        raise Denied(f"the review run {run} could not be read") from exc


def _role(value: Any, path: str) -> dict[str, str]:
    """One role of a run: its ID, prompt file, and result file, each a non-empty string, and the self-check it
    declares, when it declares one."""
    role = READ.json_object(value, path)
    for field in ("id", "prompt_file", "result_file"):
        READ.string(role.get(field), f"{path}.{field}")
    if "self_check" in role:
        READ.string(role["self_check"], f"{path}.self_check")
    return role


def _load_claim(agent: str) -> Claim | None:
    """The run and role this agent claimed, or None when it has claimed none."""
    try:
        claim = json.loads((CLAIMS / f"{agent}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Denied("this reviewer's claim could not be read") from exc
    try:
        for key in ("run", "role"):
            READ.string(READ.json_object(claim, "claim").get(key), f"claim.{key}", empty=True)
    except FieldError as exc:
        raise Denied("this reviewer's claim could not be read") from exc
    run = Path(claim["run"])
    role = next((role for role in _roles(run) if role["id"] == claim["role"]), None)
    if role is None:
        raise Denied(f"the review run no longer has the role {claim['role']}")
    return run, role


def _prune_claims() -> None:
    """Remove each claim whose run is gone: finalize removes a run's folder, run.json with it."""
    for path in CLAIMS.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            claim = json.loads(path.read_text(encoding="utf-8"))
            try:
                run: str | None = READ.string(READ.json_object(claim, "claim").get("run"), "claim.run", empty=True)
            except FieldError:
                run = None
            if run is None or not (Path(run) / RUN_FILE).is_file():
                path.unlink()


def _claim(agent: str, path: Path) -> None:
    """Bind this agent to the role whose prompt it reads first. When two first calls race, whichever wrote its
    claim first holds, and the other read is judged against that claim."""
    run = run_root(path)
    role = None if run is None else next((r for r in _roles(run) if _same(path, Path(r["prompt_file"]))), None)
    if run is None or role is None:
        raise Denied(f"{FIRST}; {path} is not a reviewer prompt")
    CLAIMS.mkdir(parents=True, exist_ok=True)
    _prune_claims()
    try:
        handle = os.open(CLAIMS / f"{agent}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        claimed = _load_claim(agent)
        if claimed is None:
            raise Denied("this reviewer's claim could not be read") from None
        _check_in_run("Read", path, claimed[0])
        return
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"run": str(run), "role": role["id"]}))
    _start_log(run, role["id"])


def _check_in_run(tool: str, path: Path, run: Path) -> None:
    if _inside(path, REFERENCES) or _inside(path, run):
        return
    raise Denied(f"{tool} may only look inside your own review run folder; {path} is outside it")


def _check_read(tool: str, tool_input: dict[str, Any], cwd: Path, claim: Claim | None) -> None:
    key = "file_path" if tool == "Read" else "path"
    path = _path(tool_input.get(key), cwd)
    if tool == "Glob":
        try:
            pattern = READ.string(tool_input.get("pattern"), "tool_input.pattern", empty=True)
        except FieldError as exc:
            raise Denied("Glob patterns must be relative to the run folder path, without '..'") from exc
        if ".." in pattern or PureWindowsPath(pattern).anchor or pattern.startswith("/"):
            raise Denied("Glob patterns must be relative to the run folder path, without '..'")
    if claim is None:
        raise Denied(f"{FIRST}; {path} is not a reviewer prompt")
    _check_in_run(tool, path, claim[0])
    _log_read(tool, path, claim)


def _check_write(tool_input: dict[str, Any], cwd: Path, claim: Claim) -> None:
    path = _path(tool_input.get("file_path"), cwd)
    result = claim[1]["result_file"]
    if not _same(path, _path(result, cwd)):
        raise Denied(f"write only your own result file, {result}; {path} is not it")


def _check_bash(tool_input: dict[str, Any], cwd: Path, claim: Claim) -> None:
    try:
        text = READ.string(tool_input.get("command"), "tool_input.command", empty=True).strip()
    except FieldError:
        text = ""  # no command, which no allowed form matches
    declared = claim[1].get("self_check")
    if declared is not None:
        if text != declared:
            raise Denied("run no command except the self-check the prompt gives, exactly as written")
        return
    match = SELF_CHECK.match(text)
    if match is not None and _same(_path(match.group("script"), cwd), PIPELINE):
        _check_own_run(match, cwd, claim, "self-check")
        return
    match = SOURCE_COMMAND.match(text)
    if (
        match is not None
        and _same(_path(match.group("script"), cwd), SOURCE_SCRIPT)
        and SOURCE_OPTIONS[match.group("command")] == match.group("option")
    ):
        _check_own_run(match, cwd, claim, match.group("command"))
        return
    raise Denied("run no command except the self-check and source commands the prompt gives, exactly as written")


def _check_own_run(match: re.Match[str], cwd: Path, claim: Claim, name: str) -> None:
    """The command, named `name` in a denial, names the reviewer's own review run folder and role."""
    run = _path(match.group("run"), cwd)
    root = run_root(run)
    if root is None or not _same(root, run):
        raise Denied(f"the {name} must name the review run folder")
    if not _same(run, claim[0]) or match.group("role") != claim[1]["id"]:
        raise Denied(f"the {name} must name your own role and review run")


def _check(tool: str, tool_input: dict[str, Any], cwd: Path, agent: str) -> None:
    claim = _load_claim(agent)
    if claim is None and tool == "Read":
        _claim(agent, _path(tool_input.get("file_path"), cwd))
    elif tool in READ_TOOLS:
        _check_read(tool, tool_input, cwd, claim)
    elif claim is None:
        raise Denied(FIRST)
    elif tool in WRITE_TOOLS:
        _check_write(tool_input, cwd, claim)
    else:
        _check_bash(tool_input, cwd, claim)


def reviewer_agent_type(plugin: str | None) -> str:
    """The subagent type a reviewer role starts as: the deployed agent's name, or a plugin's namespaced one."""
    return REVIEWER if plugin is None else f"{plugin}:{REVIEWER}"


def _another_agents_call(event: dict[str, Any], plugin: str) -> bool:
    """Whether a call the plugin's hook sees is the main session's or another agent's, which the guard leaves alone.
    Denied when it is a subagent's whose type the event does not give."""
    if event.get("agent_id") in (None, ""):
        return True
    agent_type = event.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        raise Denied("the hook event names a subagent but not its type, so the guard cannot tell a reviewer apart")
    return agent_type != reviewer_agent_type(plugin)


def decide(event: dict[str, Any], plugin: str | None = PLUGIN) -> str | None:
    """The reason to deny this tool call, or None to allow it. `plugin` is the plugin the guard runs from, whose
    hook sees every session's calls, or None for the deployed agent's own hook."""
    tool = READ.string(event.get("tool_name", ""), "hook event.tool_name", empty=True)
    if tool not in READ_TOOLS | WRITE_TOOLS | {"Bash"}:
        return None
    try:
        if plugin is not None and _another_agents_call(event, plugin):
            return None
        try:
            tool_input = READ.json_object(event.get("tool_input"), "hook event.tool_input")
        except FieldError as exc:
            raise Denied("the tool input could not be read") from exc
        try:
            agent = READ.string(event.get("agent_id"), "hook event.agent_id")
        except FieldError:
            agent = ""
        if not AGENT_ID.fullmatch(agent):
            raise Denied("the hook event carries no agent ID, so the guard cannot tell which reviewer is calling")
        cwd = READ.optional(READ.string, event.get("cwd"), "hook event.cwd", empty=True)
        _check(tool, tool_input, Path(cwd or Path.cwd()), agent)
    except Denied as exc:
        return f"Code-review reviewer boundary: {exc}."
    return None


def main(stream: str) -> int:
    try:
        try:
            event = READ.json_object(json.loads(stream), "hook event")
        except FieldError:
            event = None
        reason = "the hook event could not be read" if event is None else decide(event)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - fail closed: an unevaluated call could read the wrong code
        reason = f"Code-review reviewer boundary: the guard could not evaluate this call ({type(exc).__name__})."
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    return 0


def hook() -> int:
    """Answer the hook event on stdin. A stream that cannot be read, such as bytes the console's code page cannot
    decode, is an unreadable event, and is denied like one, never left to the runner's FAILED line, which Claude Code
    would read as a hook error and let the call through."""
    try:
        stream = sys.stdin.read()
    except (OSError, ValueError):
        stream = ""
    return main(stream)


if __name__ == "__main__":
    run_main(hook)

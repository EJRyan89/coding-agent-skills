"""PreToolUse hook for the code-review-reviewer subagent: keep a reviewer inside its own role of its review run.

A reviewer's prompt told it never to read the local checkout, and reviewers did anyway: they searched the live
working copy and the whole workspace, which may hold another branch's code. This hook enforces the boundary the
prompt describes. It reads the hook event as JSON on stdin and prints a deny decision for any call outside it:

- First, one Read of a role's prompt file, which binds the reviewer to that role and its run: its claim. The task
  names the prompt, and nothing else may be read before it, so no content from the pull request can choose the
  role. Every other call is denied until then.
- Read, Grep, and Glob only under the claimed review run folder (a `code-review-run-*` directory holding
  `run.json`: the snapshot, the trusted reviewer files, and the work files) or this skill's `references` folder.
  Grep and Glob must name that folder explicitly, because their default is the session's working directory.
- Write and Edit only on the claimed role's result file, as the run's `run.json` names it.
- Bash only for the claimed role's self-check command, exactly as the pipeline writes it, and, for a lazy snapshot,
  its `source-file` and `source-search` commands (review_source.py), with the one path or pattern the reviewer
  fills in, which may hold no quote, backtick, dollar sign, backslash, or line break.

Parallel reviewers share one session, so the hook tells them apart by the `agent_id` Claude Code puts in the hook
event of a subagent's tool call, and keeps each agent's claim in a file of its own under `CLAIMS`, which no
reviewer can write. A retry is a fresh agent that claims the same role.

The hook also counts what a reviewer reads: a claim creates the role's read log in the run's `work` folder, and each
allowed Read of a file, or Grep of one file, under the run's source snapshot appends that file's path, relative to
the snapshot, as one JSON string per line, as `source-file` does for each file it fetches or finds. Only the
pipeline's `check` reads the log, and it keeps nothing of it but counts. A log that cannot be written is left
short: counting never denies a call.

Other tools pass. Anything the hook cannot evaluate is denied, a call without an agent ID included: a reviewer
reading the wrong code, or writing another role's result, produces a plausible but wrong review, which is worse
than a failed one that check reports and retries.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import use_utf8_output

EXIT_CONTRACT_EXEMPT = "Claude Code PreToolUse hook protocol: prints a JSON decision and always exits 0"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REFERENCES = SCRIPT_DIRECTORY.parent / "references"
PIPELINE = SCRIPT_DIRECTORY / "review_pipeline.py"
RUN_PREFIX = "code-review-run-"
RUN_FILE = "run.json"
SOURCE = "source"  # the run's source snapshot
READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Write", "Edit"}
SAFE = r'[^"`$\r\n]+'
SELF_CHECK = re.compile(
    rf'^python -B "(?P<script>{SAFE})" validate-result --run "(?P<run>{SAFE})" --role "(?P<role>[a-z0-9][a-z0-9-]*)"$'
)
# The two commands review_source.py gives a lazy snapshot's reviewer. The script and the run must be the known ones,
# and no quoted value may end in a backslash, which would escape its closing quote; the path or pattern, the one value
# the reviewer chooses, holds no backslash at all, so it can never end up outside its quotes.
SOURCE_VALUE = r'[^"`$\r\n]*[^"`$\r\n\\]'
SOURCE_COMMAND = re.compile(
    rf'^python -B "(?P<script>{SOURCE_VALUE})" (?P<command>source-file|source-search) --run "(?P<run>{SOURCE_VALUE})" '
    rf'--role "(?P<role>[a-z0-9][a-z0-9-]*)" --(?P<option>path|pattern)="[^"`$\\\r\n]+"$'
)
SOURCE_OPTIONS = {"source-file": "path", "source-search": "pattern"}
SOURCE_SCRIPT = SCRIPT_DIRECTORY / "review_source.py"
SHELL_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CLAIMS = Path(tempfile.gettempdir()) / "code-review-reviewer-claims"
FIRST = "read the prompt file your task names first"
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

Claim = tuple[Path, dict[str, str]]


class Denied(Exception):
    pass


def read_log(run: Path, role: str) -> Path:
    """The file the guard logs a role's snapshot reads in. `check` in review_pipeline.py reduces it to counts."""
    return run / "work" / f"reads-{role}.log"


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
    if not isinstance(value, str) or not value.strip():
        raise Denied("give an absolute path inside the review run")
    match = SHELL_DRIVE.match(value)
    text = f"{match.group(1)}:/{value[match.end() :]}" if match else value
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.normpath(path.resolve(strict=False)))


def _same(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _inside(path: Path, root: Path) -> bool:
    root = Path(os.path.normcase(str(root.resolve(strict=False))))
    return Path(os.path.normcase(str(path))).is_relative_to(root)


def run_root(path: Path) -> Path | None:
    """The review run folder holding this path, if any."""
    for candidate in (path, *path.parents):
        if candidate.name.startswith(RUN_PREFIX) and (candidate / RUN_FILE).is_file():
            return candidate
    return None


def _roles(run: Path) -> list[dict[str, str]]:
    """The roles a prepared run's `run.json` names, each with its ID, prompt file, and result file."""
    try:
        state = json.loads((run / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Denied(f"the review run {run} could not be read") from exc
    roles = state.get("roles") if isinstance(state, dict) else None
    fields = ("id", "prompt_file", "result_file")
    if not isinstance(roles, list) or not all(
        isinstance(role, dict) and all(isinstance(role.get(field), str) and role[field] for field in fields)
        for role in roles
    ):
        raise Denied(f"the review run {run} could not be read")
    return roles


def _load_claim(agent: str) -> Claim | None:
    """The run and role this agent claimed, or None when it has claimed none."""
    try:
        claim = json.loads((CLAIMS / f"{agent}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Denied("this reviewer's claim could not be read") from exc
    if not isinstance(claim, dict) or not all(isinstance(claim.get(key), str) for key in ("run", "role")):
        raise Denied("this reviewer's claim could not be read")
    run = Path(claim["run"])
    role = next((role for role in _roles(run) if role["id"] == claim["role"]), None)
    if role is None:
        raise Denied(f"the review run no longer has the role {claim['role']}")
    return run, role


def _prune_claims() -> None:
    """Remove each claim whose run is gone: finalize removes a run's folder, run.json with it."""
    for path in CLAIMS.glob("*.json"):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            run = json.loads(path.read_text(encoding="utf-8")).get("run")
            if not isinstance(run, str) or not (Path(run) / RUN_FILE).is_file():
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
    if _inside(path, REFERENCES):
        return
    root = run_root(path)
    if root is None or not _same(root, run):
        raise Denied(f"{tool} may only look inside your own review run folder; {path} is outside it")


def _check_read(tool: str, tool_input: dict[str, Any], cwd: Path, claim: Claim | None) -> None:
    key = "file_path" if tool == "Read" else "path"
    path = _path(tool_input.get(key), cwd)
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        if (
            not isinstance(pattern, str)
            or ".." in pattern
            or PureWindowsPath(pattern).anchor
            or pattern.startswith("/")
        ):
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
    command = tool_input.get("command")
    text = command.strip() if isinstance(command, str) else ""
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


def decide(event: dict[str, Any]) -> str | None:
    """The reason to deny this tool call, or None to allow it."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input")
    if tool not in READ_TOOLS | WRITE_TOOLS | {"Bash"}:
        return None
    try:
        if not isinstance(tool_input, dict):
            raise Denied("the tool input could not be read")
        agent = event.get("agent_id")
        if not isinstance(agent, str) or not AGENT_ID.fullmatch(agent):
            raise Denied("the hook event carries no agent ID, so the guard cannot tell which reviewer is calling")
        _check(tool, tool_input, Path(event.get("cwd") or Path.cwd()), agent)
    except Denied as exc:
        return f"Code-review reviewer boundary: {exc}."
    return None


def main(stream: str) -> int:
    try:
        event = json.loads(stream)
        reason = decide(event) if isinstance(event, dict) else "the hook event could not be read"
    except Exception as exc:  # fail closed: an unevaluated call could read the wrong code
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


if __name__ == "__main__":
    use_utf8_output()
    sys.exit(main(sys.stdin.read()))

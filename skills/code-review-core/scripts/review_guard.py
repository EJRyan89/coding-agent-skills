"""PreToolUse hook for the code-review-reviewer subagent: keep a reviewer inside its review run.

A reviewer's prompt told it never to read the local checkout, and reviewers did anyway: they searched the live
working copy and the whole workspace, which may hold another branch's code. This hook enforces the boundary the
prompt describes. It reads the hook event as JSON on stdin and prints a deny decision for any call outside it:

- Read, Grep, and Glob only under a review run folder (a `code-review-run-*` directory holding `run.json`: the
  snapshot, the trusted reviewer files, and the work files) or this skill's `references` folder. Grep and Glob
  must name that folder explicitly, because their default is the session's working directory.
- Write and Edit only on a result file: `<run>/result.json` or `<run>/work/<role>.result.json`.
- Bash only for the self-check command the prompt gives, exactly as the pipeline writes it.

Other tools pass. Anything the hook cannot evaluate is denied: a reviewer reading the wrong code produces a
plausible but wrong review, which is worse than a failed one that check reports and retries.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REFERENCES = SCRIPT_DIRECTORY.parent / "references"
PIPELINE = SCRIPT_DIRECTORY / "review_pipeline.py"
RUN_PREFIX = "code-review-run-"
RUN_FILE = "run.json"
READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Write", "Edit"}
SAFE = r'[^"`$\r\n]+'
SELF_CHECK = re.compile(
    rf'^python -B "(?P<script>{SAFE})" validate-result --run "(?P<run>{SAFE})" --role "(?P<role>[a-z0-9][a-z0-9-]*)"$'
)
SHELL_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")


class Denied(Exception):
    pass


def _path(value: Any, cwd: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Denied("give an absolute path inside the review run")
    match = SHELL_DRIVE.match(value)
    text = f"{match.group(1)}:/{value[match.end():]}" if match else value
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


def _check_read(tool: str, tool_input: dict[str, Any], cwd: Path) -> None:
    key = "file_path" if tool == "Read" else "path"
    path = _path(tool_input.get(key), cwd)
    if run_root(path) is None and not _inside(path, REFERENCES):
        raise Denied(f"{tool} may only look inside the review run folder; {path} is outside it")
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or ".." in pattern or PureWindowsPath(pattern).anchor or pattern.startswith("/"):
            raise Denied("Glob patterns must be relative to the run folder path, without '..'")


def _check_write(tool_input: dict[str, Any], cwd: Path) -> None:
    path = _path(tool_input.get("file_path"), cwd)
    run = run_root(path)
    allowed = run is not None and (
        _same(path, run / "result.json")
        or (_same(path.parent, run / "work") and path.name.endswith(".result.json"))
    )
    if not allowed:
        raise Denied(f"write only the result file the prompt names; {path} is not one")


def _check_bash(tool_input: dict[str, Any], cwd: Path) -> None:
    command = tool_input.get("command")
    match = SELF_CHECK.match(command.strip()) if isinstance(command, str) else None
    if match is None or not _same(_path(match.group("script"), cwd), PIPELINE):
        raise Denied("run no command except the self-check command the prompt gives, exactly as written")
    run = _path(match.group("run"), cwd)
    if run_root(run) is None or not _same(run_root(run), run):
        raise Denied("the self-check must name the review run folder")


def decide(event: dict[str, Any]) -> str | None:
    """The reason to deny this tool call, or None to allow it."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input")
    if tool not in READ_TOOLS | WRITE_TOOLS | {"Bash"}:
        return None
    try:
        if not isinstance(tool_input, dict):
            raise Denied("the tool input could not be read")
        cwd = Path(event.get("cwd") or os.getcwd())
        if tool in READ_TOOLS:
            _check_read(tool, tool_input, cwd)
        elif tool in WRITE_TOOLS:
            _check_write(tool_input, cwd)
        else:
            _check_bash(tool_input, cwd)
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
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))

"""List the skills Codex and GitHub Copilot CLI discover, without starting a model.

Copilot CLI prints its skills with `copilot skill list --json`, one entry per name: the copy it will use. Codex CLI
has no listing command; its app server answers a `skills/list` request, a JSON-RPC message on stdin, with every copy
it finds, user-only skills included, and needs no sign-in to do so. `codex debug prompt-input` is no substitute: it
lists only the skills the model may choose for itself, so it leaves user-only skills out.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import platform_support

RUNTIMES = ("codex", "copilot")
LABELS = {"codex": "Codex CLI", "copilot": "Copilot CLI"}
DEFAULT_TIMEOUT = 120
# How long a program may take to exit once its stdin closes, before it is killed.
CLOSE_GRACE = 5
CODEX_LIST_ID = 1


@dataclass(frozen=True)
class Listed:
    """One copy of a skill a runtime lists: its directory, with forward slashes, and whether it is enabled."""

    directory: str
    enabled: bool


Listing = dict[str, list[Listed]]


class ListingError(Exception):
    """The runtime could not list its skills; the message says why."""


# Starts a program in a directory with an environment, writes each request as a line on its stdin, and returns its
# stdout lines up to and including the first one `answered` accepts, or to the end when there is none.
Converse = Callable[[list[str], Path, Mapping[str, str], list[str], "Callable[[str], bool] | None", float], list[str]]


def converse(
    arguments: list[str],
    cwd: Path,
    environment: Mapping[str, str],
    requests: list[str],
    answered: Callable[[str], bool] | None,
    timeout: float,
) -> list[str]:
    lines: queue.Queue[str | None] = queue.Queue()
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                arguments,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise ListingError(f"cannot start it: {exc}") from exc

        def read() -> None:
            with process.stdout:
                for line in process.stdout:
                    lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        received: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            # A program that has already exited refuses its requests; its exit code and errors say why.
            with contextlib.suppress(OSError):
                for request in requests:
                    process.stdin.write(request + "\n")
                process.stdin.flush()
                if answered is None:
                    # The Codex app server stops at the end of stdin, before it answers, so stdin stays open
                    # until an awaited answer arrives.
                    process.stdin.close()
            while True:
                try:
                    line = lines.get(timeout=max(deadline - time.monotonic(), 0))
                except queue.Empty:
                    process.kill()
                    raise ListingError(f"no answer within {timeout:g} seconds") from None
                if line is None:
                    break
                received.append(line.rstrip("\n"))
                if answered is not None and answered(line):
                    return received
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=CLOSE_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=CLOSE_GRACE)
            errors.seek(0)
            stderr = errors.read().decode("utf-8", "replace").strip()
    if answered is not None:
        raise ListingError(_ended("it exited before answering", process.returncode, stderr))
    if process.returncode != 0:
        raise ListingError(_ended("it failed", process.returncode, stderr))
    return received


def _ended(what: str, code: int, stderr: str) -> str:
    last = stderr.splitlines()[-1] if stderr else ""
    return f"{what} with exit code {code}" + (f": {last}" if last else "")


def _directory(path: str) -> str:
    return platform_support.normalize(os.path.normpath(path))


def parse_copilot(output: str) -> Listing:
    """The skills in `copilot skill list --json` output, whose paths are skill directories."""
    try:
        entries = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ListingError(f"its skill list is not JSON: {exc}") from None
    if isinstance(entries, dict):
        entries = entries.get("skills")
    if not isinstance(entries, list):
        raise ListingError("its skill list is not a JSON array")
    found: Listing = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("path"), str):
            found.setdefault(entry["name"], []).append(Listed(_directory(entry["path"]), entry.get("enabled") is True))
    return found


def codex_requests(cwd: Path) -> list[str]:
    """The app server's handshake, then a request for the skills it finds from `cwd`."""
    messages = [
        {
            "method": "initialize",
            "id": 0,
            "params": {"clientInfo": {"name": "coding-agent-skills", "title": "coding-agent-skills", "version": "1"}},
        },
        {"method": "initialized"},
        {"method": "skills/list", "id": CODEX_LIST_ID, "params": {"cwds": [str(cwd)]}},
    ]
    return [json.dumps(message) for message in messages]


def _codex_message(line: str) -> dict | None:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def codex_answered(line: str) -> bool:
    message = _codex_message(line)
    return message is not None and message.get("id") == CODEX_LIST_ID


def parse_codex(lines: list[str]) -> Listing:
    """The skills in the app server's answer to `skills/list`, whose paths are SKILL.md files."""
    answers = [message for line in lines if (message := _codex_message(line)) and message.get("id") == CODEX_LIST_ID]
    if not answers:
        raise ListingError("it did not answer the skills/list request")
    answer = answers[-1]
    if "error" in answer:
        error = answer["error"]
        reason = error.get("message") if isinstance(error, dict) else error
        raise ListingError(f"it refused the skills/list request: {reason}")
    result = answer.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        raise ListingError("its skills/list answer has no data")
    found: Listing = {}
    for group in data:
        for entry in (group.get("skills") or []) if isinstance(group, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("path"), str):
                directory = _directory(os.path.dirname(entry["path"]))
                found.setdefault(entry["name"], []).append(Listed(directory, entry.get("enabled") is True))
    return found


def list_skills(
    runtime: str,
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    talk: Converse | None = None,
) -> Listing:
    """The skills `runtime` finds when started in `cwd`; raise ListingError when it cannot say."""
    talk = talk or converse
    if runtime == "codex":
        return parse_codex(
            talk([executable, "app-server"], cwd, environment, codex_requests(cwd), codex_answered, timeout)
        )
    if runtime == "copilot":
        return parse_copilot(
            "\n".join(talk([executable, "skill", "list", "--json"], cwd, environment, [], None, timeout))
        )
    raise ValueError(f"no skill listing for {runtime}")

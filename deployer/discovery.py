"""List the skills Codex and GitHub Copilot CLI discover, without starting a model.

Copilot CLI prints its skills with `copilot skill list --json`, one entry per name: the copy it will use. Codex CLI
has no listing command; its app server answers a `skills/list` request, a JSON-RPC message on stdin, with every copy
it finds, user-only skills included, and those it could not load with its reasons, and needs no sign-in to do so. The
app server is experimental, so a Codex release may change that answer. `codex debug prompt-input` is no substitute: it
lists only the skills the model may choose for itself, so it leaves user-only skills out, which is what makes it the
check that a user-only skill stays out of the model's catalog.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from bounded_process import ProcessTree, without_repository_variables

from . import platform_support

# Each runtime's listing names its field skills, a word the manifest also uses as a key; neither listing is a manifest.
KINDS_ALLOWED = {
    "parse_copilot": "reads Copilot CLI's skill listing, whose field is named skills",
    "parse_codex": "reads the Codex app server's skills/list answer, whose field is named skills",
}

RUNTIMES = ("codex", "copilot")
LABELS = {"codex": "Codex CLI", "copilot": "Copilot CLI"}
DEFAULT_TIMEOUT = 120
# How long a program may take to exit once its stdin closes, before it is killed.
CLOSE_GRACE = 5
CODEX_LIST_ID = 1
# The model-free view of the skills Codex offers the model, which leaves out a skill only the user may start.
CODEX_CATALOG = ("debug", "prompt-input")
CODEX_CATALOG_ROOT = re.compile(r"^- `(r\d+)` = `([^`]+)`$", re.MULTILINE)
CODEX_CATALOG_SKILL = re.compile(r"^- ([\w.-]+): .*\(file: ([^)\s]+)\)$", re.MULTILINE)
# The heading of the skills message's list of skills, under which every list item is one skill.
CODEX_CATALOG_SKILLS = "### Available skills"
# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.TemporaryFile": "a runtime's stderr is captured in an unnamed file under the system temporary directory, "
    "outside every managed root, which closing it deletes; it changes no deployed state",
}


@dataclass(frozen=True)
class Listed:
    """One copy of a skill a runtime lists: its directory, with forward slashes, and whether it is enabled."""

    directory: str
    enabled: bool


Listing = dict[str, list[Listed]]


@dataclass(frozen=True)
class Refused:
    """A skill a runtime found but could not load: its directory, with forward slashes, and the runtime's reason."""

    directory: str
    reason: str


@dataclass(frozen=True)
class Discovered:
    """What a runtime lists, and the skills it found but could not load."""

    listing: Listing
    refused: tuple[Refused, ...] = ()


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
    with tempfile.TemporaryFile() as errors, contextlib.ExitStack() as held:
        tree = platform_support.process_tree()
        process, stdin, stdout = _start(arguments, cwd, environment, errors, tree)
        # Leaving the tree ends whatever the program started, such as the runtime a launcher script runs, which a kill
        # of the program alone would leave running.
        _hold(held, tree, process, stdin, stdout)
        reader = threading.Thread(target=_read_lines, args=(stdout, lines), daemon=True)
        reader.start()
        received: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            _send(stdin, requests, answered)
            if _receive(lines, process, received, answered, deadline, timeout):
                return received
        finally:
            stderr = _close(process, stdin, reader, errors)
    if answered is not None:
        raise ListingError(_ended("it exited before answering", process.returncode, stderr))
    if process.returncode != 0:
        raise ListingError(_ended("it failed", process.returncode, stderr))
    return received


def _start(
    arguments: list[str], cwd: Path, environment: Mapping[str, str], errors: IO[bytes], tree: ProcessTree
) -> tuple[subprocess.Popen[str], IO[str], IO[str]]:
    """The started program and its stdin and stdout pipes, its stderr going to errors, started as `tree` holds it.

    Its environment is `environment` without git's repository-selecting variables, so a runtime lists the skills of
    `cwd` and the home, never those of a repository a shell's `GIT_DIR` names.
    """
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=without_repository_variables(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            errors="replace",
            **tree.start_options(),
        )
    except OSError as exc:
        raise ListingError(f"cannot start it: {exc}") from exc
    stdin, stdout = process.stdin, process.stdout
    # Both were requested as pipes above; a program started without them cannot be asked anything.
    if stdin is None or stdout is None:
        process.kill()
        process.wait()
        raise ListingError("its pipes did not open")
    return process, stdin, stdout


def _hold(
    held: contextlib.ExitStack, tree: ProcessTree, process: subprocess.Popen[str], stdin: IO[str], stdout: IO[str]
) -> None:
    """Hold the started program in `tree` until `held` closes; one that cannot be held is ended."""
    try:
        held.enter_context(tree.hold(process))
    except OSError as exc:
        for pipe in (stdin, stdout):
            with contextlib.suppress(OSError):
                pipe.close()
        raise ListingError(f"cannot end what it starts: {exc}") from exc


def _read_lines(stdout: IO[str], lines: queue.Queue[str | None]) -> None:
    """Queue each line the program prints, then None at the end of its output."""
    with stdout:
        for line in stdout:
            lines.put(line)
    lines.put(None)


def _send(stdin: IO[str], requests: list[str], answered: Callable[[str], bool] | None) -> None:
    # A program that has already exited refuses its requests; its exit code and errors say why.
    with contextlib.suppress(OSError):
        for request in requests:
            stdin.write(request + "\n")
        stdin.flush()
        if answered is None:
            # The Codex app server stops at the end of stdin, before it answers, so stdin stays open
            # until an awaited answer arrives.
            stdin.close()


def _receive(
    lines: queue.Queue[str | None],
    process: subprocess.Popen[str],
    received: list[str],
    answered: Callable[[str], bool] | None,
    deadline: float,
    timeout: float,
) -> bool:
    """Add each line to received until answered accepts one, which returns True, or the output ends."""
    while True:
        try:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0))
        except queue.Empty:
            process.kill()
            raise ListingError(f"no answer within {timeout:g} seconds") from None
        if line is None:
            return False
        received.append(line.rstrip("\n"))
        if answered is not None and answered(line):
            return True


def _close(process: subprocess.Popen[str], stdin: IO[str], reader: threading.Thread, errors: IO[bytes]) -> str:
    """Close stdin, wait for the program (killing it after the grace), and return what it wrote to stderr."""
    with contextlib.suppress(OSError):
        stdin.close()
    try:
        process.wait(timeout=CLOSE_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    reader.join(timeout=CLOSE_GRACE)
    errors.seek(0)
    return errors.read().decode("utf-8", "replace").strip()


def _ended(what: str, code: int, stderr: str) -> str:
    last = stderr.splitlines()[-1] if stderr else ""
    return f"{what} with exit code {code}" + (f": {last}" if last else "")


def _directory(path: str) -> str:
    return platform_support.normalize(os.path.normpath(path))


def parse_copilot(output: str) -> Listing:
    """The skills in `copilot skill list --json` output, whose paths are skill directories."""
    try:
        entries = json.loads(output)
    except (ValueError, RecursionError) as exc:  # a decoding error or the digit limit, or nesting too deep
        raise ListingError(f"its skill list is not JSON: {exc}") from None
    if isinstance(entries, dict):
        entries = entries.get("skills")
    if not isinstance(entries, list):
        raise ListingError("its skill list is not a JSON array")
    found: Listing = {}
    for entry in entries:
        name, path, enabled = _skill(entry, "its skill list")
        found.setdefault(name, []).append(Listed(_directory(path), enabled))
    return found


def _shape(entry: object) -> str:
    """What an entry holds, for a refusal: its keys, or its JSON type."""
    if isinstance(entry, dict):
        return f"an object with {', '.join(sorted(map(str, entry))) or 'no key'}"
    return "a list" if isinstance(entry, list) else type(entry).__name__


def _skill(entry: object, where: str) -> tuple[str, str, bool]:
    """A listed skill's name, path, and whether it is enabled. An entry that is not an object with a string name and
    path and a boolean enabled is refused, since a listing read past it would report the skill missing or turned off
    when the runtime said something this reader does not know."""
    if isinstance(entry, dict):
        name, path, enabled = entry.get("name"), entry.get("path"), entry.get("enabled")
        if isinstance(name, str) and isinstance(path, str) and isinstance(enabled, bool):
            return name, path, enabled
    raise ListingError(f"{where} has an entry of a shape this reader does not know: {_shape(entry)}")


def codex_requests(cwd: Path) -> list[str]:
    """The app server's handshake, then a request for the skills it finds from `cwd`."""
    messages = [
        {
            "method": "initialize",
            "id": 0,
            "params": {"clientInfo": {"name": "coding-agent-skills", "title": "coding-agent-skills", "version": "1"}},
        },
        {"method": "initialized", "params": {}},
        {"method": "skills/list", "id": CODEX_LIST_ID, "params": {"cwds": [str(cwd)]}},
    ]
    return [json.dumps(message) for message in messages]


def _codex_message(line: str) -> dict[str, object] | None:
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):  # a decoding error or the digit limit, or nesting too deep
        return None
    return message if isinstance(message, dict) else None


def codex_answered(line: str) -> bool:
    message = _codex_message(line)
    return message is not None and message.get("id") == CODEX_LIST_ID


def _codex_groups(lines: list[str]) -> list[dict[str, object]]:
    """The groups of the app server's answer to `skills/list`, one per working directory asked about."""
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
    return [group for group in data if isinstance(group, dict)]


def _entries(group: dict[str, object], field: str) -> list[object]:
    """The entries of a group's field, none when the group leaves it out; a field of another shape is refused."""
    listed = group.get(field)
    if listed is None:
        return []
    if not isinstance(listed, list):
        raise ListingError(f"its skills/list answer's {field} is {_shape(listed)}, not an array")
    return listed


def parse_codex(lines: list[str]) -> Listing:
    """The skills in the app server's answer to `skills/list`, whose paths are SKILL.md files."""
    found: Listing = {}
    for group in _codex_groups(lines):
        for entry in _entries(group, "skills"):
            name, path, enabled = _skill(entry, "its skills/list answer")
            found.setdefault(name, []).append(Listed(_directory(str(Path(path).parent)), enabled))
    return found


def parse_codex_refusals(lines: list[str]) -> tuple[Refused, ...]:
    """The skill files the app server's answer to `skills/list` says Codex could not load, with its reasons."""
    refused: list[Refused] = []
    for group in _codex_groups(lines):
        for entry in _entries(group, "errors"):
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(path, str):
                raise ListingError(
                    f"its skills/list answer has an error of a shape this reader does not know: {_shape(entry)}"
                )
            reason = str(entry.get("message") or "no reason given")
            refused.append(Refused(_directory(str(Path(path).parent)), reason))
    return tuple(refused)


def parse_codex_catalog(lines: list[str]) -> Listing:
    """The skills `codex debug prompt-input` offers the model, by the directories of their SKILL.md files.

    Its output is a JSON list of the messages Codex would send. The skills message names each root as
    "- `r0` = `<directory>`" and each skill as "- <name>: <description> (file: r0/<name>/SKILL.md)", or, without a
    roots table, by the file's absolute path. Output without that message, or whose message lists no skill, is
    refused: Codex lists its own system skills there, so either says Codex wrote its prompt in a shape this reader does
    not know, and an empty catalog read from it would pass every user-only adapter as kept out.
    """
    try:
        messages = json.loads("\n".join(lines))
        texts = _strings(messages)
    except (ValueError, RecursionError) as exc:  # a decoding error or the digit limit, or nesting too deep
        raise ListingError(f"its prompt input is not JSON: {exc}") from None
    skills = [text for text in texts if CODEX_CATALOG_SKILLS in text]
    if not skills:
        raise ListingError(f"its prompt input has no skills message, which lists them under {CODEX_CATALOG_SKILLS}")
    found: Listing = {}
    for text in skills:
        roots = dict(CODEX_CATALOG_ROOT.findall(text))
        for name, file in _catalog_skills(text):
            alias, _, rest = file.partition("/")
            path = f"{roots[alias]}/{rest}" if alias in roots else file
            found.setdefault(name, []).append(Listed(_directory(str(Path(path).parent)), True))
    if not found:
        raise ListingError(f"its prompt input lists no skill under {CODEX_CATALOG_SKILLS}")
    return found


def _catalog_skills(text: str) -> list[tuple[str, str]]:
    """Each skill a skills message lists, as its name and file; a list item under its "### Available skills" that
    is not of that shape is refused, since passing over it would report the skill out of the model's catalog."""
    skills: list[tuple[str, str]] = []
    for line in text.split(CODEX_CATALOG_SKILLS, 1)[1].splitlines():
        if line.startswith(("#", "<")):
            break
        if line.startswith("- "):
            listed = CODEX_CATALOG_SKILL.match(line)
            if listed is None:
                raise ListingError(f"its prompt input lists a skill in a shape this reader does not know: {line}")
            skills.append((listed[1], listed[2]))
    return skills


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    values = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    return [text for value in values for text in _strings(value)]


def list_skills(
    runtime: str,
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    talk: Converse | None = None,
) -> Listing:
    """The skills `runtime` finds when started in `cwd`; raise ListingError when it cannot say."""
    return discover(runtime, executable, cwd, environment, timeout, talk).listing


def discover(
    runtime: str,
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    talk: Converse | None = None,
) -> Discovered:
    """The skills `runtime` finds when started in `cwd`, with those it could not load where it says so (Codex);
    raise ListingError when it cannot say."""
    talk = talk or converse
    command = listing_command(runtime, executable)
    if runtime == "codex":
        lines = talk(command, cwd, environment, codex_requests(cwd), codex_answered, timeout)
        return Discovered(parse_codex(lines), parse_codex_refusals(lines))
    return Discovered(parse_copilot("\n".join(talk(command, cwd, environment, [], None, timeout))))


def listing_command(runtime: str, executable: str) -> list[str]:
    """The command that lists the skills `runtime` finds: Codex's app server, which answers `skills/list` on stdin,
    or Copilot CLI's `skill list --json`."""
    if runtime == "codex":
        return [executable, "app-server"]
    if runtime == "copilot":
        return [executable, "skill", "list", "--json"]
    raise ValueError(f"no skill listing for {runtime}")


def catalog_command(executable: str) -> list[str]:
    """The command that prints the skills Codex offers the model, which starts no model."""
    return [executable, *CODEX_CATALOG]


def codex_catalog(
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    talk: Converse | None = None,
) -> Listing:
    """The skills Codex offers the model when started in `cwd`, from `codex debug prompt-input`, which starts no
    model; raise ListingError when it cannot say."""
    talk = talk or converse
    return parse_codex_catalog(talk(catalog_command(executable), cwd, environment, [], None, timeout))

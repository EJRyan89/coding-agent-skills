"""Coordinate parallel sessions: one hub checkout that stays on main, one worktree per task under .claude/worktrees.

Usage:
  python tools/worktrees.py new <kind> <name>   create .claude/worktrees/<kind>-<name> on branch <kind>/<name>
  python tools/worktrees.py list                one row per worktree: branch, state, ahead/behind main, dirt
  python tools/worktrees.py guard               Claude Code PreToolUse hook; reads the hook JSON on stdin

The guard refuses file edits inside the hub and git commands that would move the hub's HEAD or write its tree,
whether they run through the Bash tool or the PowerShell tool. A PowerShell command is read by PowerShell's own
parser (tools/worktrees-powershell.ps1); every decision is made here. It is inert unless
`git config coding-agent-skills.hubGuard true` was set in this clone. See docs/parallel-sessions.md.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployer import platform_support

MAIN = "main"
# Where Claude Code's EnterWorktree and the desktop app create session worktrees; ignored by the hub's .gitignore.
WORKTREES = Path(".claude") / "worktrees"
GUARD_SETTING = "coding-agent-skills.hubGuard"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FILE_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
SEPARATOR_CHARACTERS = set(";&|()\n")
# Git subcommands that move HEAD, or write the index or working tree, of the checkout they run in.
WRITING_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "pull",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
}
# git's own options that take the next word as their value.
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--config-env", "--git-dir", "--namespace", "--super-prefix", "--work-tree"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GIT_NAMES = {"git"}
BASH_NAMES = {"bash", "sh"}
POWERSHELL_NAMES = {"pwsh", "powershell"}
LOCATION_NAMES = {"cd", "chdir", "sl", "set-location"}
PUSH_LOCATION_NAMES = {"pushd", "push-location"}
POP_LOCATION_NAMES = {"popd", "pop-location"}
POWERSHELL_COMMAND_SWITCH = re.compile(r"-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?", re.IGNORECASE)
# How deep bash -c, pwsh -Command and Invoke-Expression are followed; deeper nesting is left unread.
NESTING_LIMIT = 4
POWERSHELL_READER = Path(__file__).with_name("worktrees-powershell.ps1")
# Bounds only a pwsh that hangs: reading is linear in the command. Generous, so a loaded machine cannot turn a
# refusal into an allowance, and well inside the hook's own 30-second timeout in .claude/settings.json.
POWERSHELL_TIMEOUT_SECONDS = 20
MENTIONS_GIT = re.compile(r"git", re.IGNORECASE)
MENTIONS_WRITING_SUBCOMMAND = re.compile(
    r"\b(?:" + "|".join(map(re.escape, sorted(WRITING_SUBCOMMANDS))) + r")\b", re.IGNORECASE
)
DENY_REASON = (
    "This checkout is the hub: it stays on main and is never edited, so parallel sessions cannot carry each "
    "other's changes (docs/parallel-sessions.md). Create a worktree with "
    "`python tools/worktrees.py new <kind> <name>`, enter it, and work there."
)


class UsageError(Exception):
    pass


def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


@dataclass(frozen=True)
class Checkout:
    toplevel: Path
    hub: Path

    @property
    def is_hub(self) -> bool:
        return same_path(self.toplevel, self.hub)


def checkout_of(path: Path) -> Checkout | None:
    """The worktree containing path, and the hub that owns it; None outside a non-bare repository."""
    directory = path
    while not directory.is_dir():
        if directory.parent == directory:
            return None
        directory = directory.parent
    result = git(directory, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    common = Path(lines[1])
    if common.name != ".git":
        return None
    return Checkout(Path(lines[0]), common.parent)


def guard_enabled(hub: Path) -> bool:
    return git(hub, "config", "--type=bool", "--get", GUARD_SETTING).stdout.strip() == "true"


def resolve(value: str, cwd: Path) -> Path:
    path = Path(platform_support.from_shell_path(value))
    return path if path.is_absolute() else cwd / path


def hub_protected(path: Path) -> bool:
    """Whether path lies in the tracked surface of a guarded hub; ignored files such as local settings do not."""
    checkout = checkout_of(path)
    if checkout is None or not checkout.is_hub or not guard_enabled(checkout.hub):
        return False
    return git(checkout.hub, "check-ignore", "-q", "--no-index", str(path)).returncode != 0


# ---------------------------------------------------------------------------------------------------------------
# Command parsing


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands at ; & | ( ) and newlines, tokenized as the shell would.

    A heredoc body or multi-line string can leave quotes unbalanced across the whole command; then each line is
    tokenized on its own and a line that still cannot be is skipped, so `git commit -F - <<'EOF'` is still seen.
    """
    try:
        return _tokenize(command)
    except ValueError:
        found: list[list[str]] = []
        for line in command.splitlines():
            try:
                found.extend(_tokenize(line))
            except ValueError:
                continue
        return found


def _tokenize(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    found: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(";&|()<>\n") and set(token) & SEPARATOR_CHARACTERS:
            found.append([])
        else:
            found[-1].append(token)
    return [segment for segment in found if segment]


@dataclass(frozen=True)
class GitCall:
    directory: Path | None  # None when the command's text does not settle where git runs
    subcommand: str
    arguments: list[str]


@dataclass
class Reading:
    """What the guard read from one command: the git calls it makes, in order, and what it could not judge."""

    environment: Mapping[str, str]
    calls: list[GitCall] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# A word is its text, or None when the text does not settle its value (an unset variable, a $( ) result).
Word = str | None


def moved(directory: Path | None, target: Word) -> Path | None:
    """directory after moving to target; an absolute target settles it even when directory is unknown."""
    if target is None:
        return None
    path = Path(platform_support.from_shell_path(target))
    if path.is_absolute():
        return path
    return None if directory is None else directory / path


def command_name(word: Word) -> str | None:
    """The lower-cased name a command is called by, without its directory or .exe; None when it is computed."""
    return None if word is None else Path(word).name.lower().removesuffix(".exe")


def record_git_call(arguments: list[Word], directory: Path | None, reading: Reading) -> None:
    """Reads git's own options and its subcommand: -C and --work-tree move it, --git-dir leaves it unknown."""
    work_tree_named = False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        value = arguments[index + 1] if index + 1 < len(arguments) else None
        if word is None:
            reading.notes.append("a git subcommand is computed at run time")
            return
        if not word.startswith("-"):
            # A computed argument is kept as "", which no allowance matches.
            reading.calls.append(GitCall(directory, word, [argument or "" for argument in arguments[index + 1 :]]))
            return
        option, inline, inline_value = word.partition("=")
        if word == "-C":
            directory = moved(directory, value)
        elif option == "--work-tree":
            directory = moved(directory, inline_value if inline else value)
            work_tree_named = True
        elif option == "--git-dir" and not work_tree_named:
            directory = None
        index += 2 if word in GIT_OPTIONS_WITH_VALUE else 1


def follow(
    name: str, script: list[Word] | None, cwd: Path | None, reading: Reading, depth: int, read: ShellReader
) -> None:
    """Reads the script another shell is handed as text. It runs as a process of its own, so its cd stays there."""
    if script is None:
        return
    if not script or None in script or cwd is None or depth >= NESTING_LIMIT:
        reading.notes.append(f"could not read the command passed to {name}")
        return
    read(" ".join(word for word in script if word is not None), cwd, reading, depth + 1)


def bash_script(arguments: list[Word]) -> list[Word] | None:
    """The one word `bash -c` runs; None when it runs no inline script."""
    return arguments[1:2] if arguments[:1] == ["-c"] else None


def powershell_script(arguments: list[Word]) -> list[Word] | None:
    """Every word after pwsh's -Command, or an abbreviation of it; None when it runs no inline script."""
    for index, word in enumerate(arguments):
        if word is not None and POWERSHELL_COMMAND_SWITCH.fullmatch(word):
            return arguments[index + 1 :]
    return None


def read_bash(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    for segment in segments(command):
        if segment[0] == "cd" and len(segment) == 2:
            cwd = moved(cwd, segment[1])
            continue
        directory, index = cwd, 0
        work_tree_named = False
        while index < len(segment) and ASSIGNMENT.match(segment[index]):
            name, _, value = segment[index].partition("=")
            if name == "GIT_WORK_TREE":
                directory, work_tree_named = moved(cwd, value), True
            elif name == "GIT_DIR" and not work_tree_named:
                directory = None
            index += 1
        if index >= len(segment):
            continue
        program = command_name(segment[index])
        arguments: list[Word] = list(segment[index + 1 :])
        if program in GIT_NAMES:
            record_git_call(arguments, directory, reading)
        elif program in BASH_NAMES:
            follow(program, bash_script(arguments), cwd, reading, depth, read_bash)
        elif program in POWERSHELL_NAMES:
            follow(program, powershell_script(arguments), cwd, reading, depth, read_powershell)


@functools.cache
def powershell_reader() -> str:
    return POWERSHELL_READER.read_text(encoding="utf-8")


def powershell_events(command: str, reading: Reading) -> list[dict[str, Any]] | None:
    """The reader's events for one command, or None with a note saying why there are none.

    pwsh starts with -Command rather than -File, so no execution policy applies, and not with -EncodedCommand,
    which endpoint protection treats as a sign of malware. The command travels base64-encoded on stdin, past any
    console code page.
    """
    pwsh = platform_support.find_pwsh(reading.environment.get("PATH", ""))
    if pwsh is None:
        reading.notes.append("could not find pwsh to read a PowerShell command")
        return None
    try:
        result = subprocess.run(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershell_reader()],
            input=base64.b64encode(command.encode("utf-8")).decode("ascii"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(reading.environment),
            timeout=POWERSHELL_TIMEOUT_SECONDS,
            check=False,
            **platform_support.hidden_window(),
        )
    except subprocess.TimeoutExpired:
        reading.notes.append(f"pwsh did not read the command within {POWERSHELL_TIMEOUT_SECONDS} seconds")
        return None
    except OSError as exc:
        reading.notes.append(f"could not start pwsh ({exc})")
        return None
    try:
        output = json.loads(result.stdout)
    except ValueError:
        reading.notes.append(f"pwsh could not read the command ({(result.stderr.strip().splitlines() or [''])[0]})")
        return None
    if "error" in output:
        # PowerShell parses a whole script before it runs any of it, so a command that does not parse runs
        # nothing; a Bash command's lines before a syntax error do run.
        reading.notes.append(f"PowerShell would not parse the command, so none of it runs ({output['error']})")
        return None
    return output["events"]


def location_target(arguments: list[Word]) -> tuple[bool, Word]:
    """Whether a Set-Location or Push-Location call names a path, and that path (None when it is not settled).

    The path is the -Path or -LiteralPath value, either of which PowerShell lets be abbreviated, or the first
    positional word.
    """
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith("-") or word == "-":
            return True, word
        name = word[1:].lower()
        if name and ("path".startswith(name) or "literalpath".startswith(name) or name in {"pspath", "lp"}):
            return True, arguments[index + 1] if index + 1 < len(arguments) else None
        index += 2 if name and "stackname".startswith(name) else 1
    return False, None


def location_after(cwd: Path | None, named: bool, target: Word) -> Path | None:
    """Where Set-Location leaves the session. Bare, it goes home; `-` and `+` walk a history the text does not show."""
    if not named:
        return Path.home()
    if target is None or target in {"-", "+"}:
        return None
    if target == "~" or target.startswith(("~/", "~\\")):
        # PowerShell's file system provider reads a leading ~ as home; the parser leaves it as text.
        return Path.home() / target[2:]
    return moved(cwd, target)


@dataclass
class Session:
    """A PowerShell session's location, its Push-Location stack, and whether git's own variables were set."""

    cwd: Path | None
    stack: list[Path | None] = field(default_factory=list)
    git_environment: bool = False


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a PowerShell command with PowerShell's own parser, started only for text it could refuse.

    Starting pwsh costs far more than the guard's git calls, so text that does not name git and a refused
    subcommand never pays for it. A location belongs to the session, not to a script block's scope, so only a
    child pwsh, which the reader brackets with push and pop, restores one.
    """
    if not (MENTIONS_GIT.search(command) and MENTIONS_WRITING_SUBCOMMAND.search(command)):
        return
    events = powershell_events(command, reading)
    if events is None:
        return
    parents: list[Session] = []
    session = Session(cwd)
    for event in events:
        kind = event.get("kind")
        if kind == "push":
            parents.append(session)
            session = Session(session.cwd, git_environment=session.git_environment)
        elif kind == "pop":
            session = parents.pop() if parents else session
        elif kind == "gitEnv":
            session.git_environment = True
        elif kind == "note":
            reading.notes.append(str(event.get("text")))
        elif kind == "command":
            words: list[Word] = [None if word["dynamic"] else word["text"] for word in event["words"]]
            name, arguments = command_name(words[0]), words[1:]
            if name in LOCATION_NAMES:
                session.cwd = location_after(session.cwd, *location_target(arguments))
            elif name in PUSH_LOCATION_NAMES:
                session.stack.append(session.cwd)
                named, target = location_target(arguments)
                if named:
                    session.cwd = location_after(session.cwd, named, target)
            elif name in POP_LOCATION_NAMES:
                session.cwd = session.stack.pop() if session.stack else None
            elif name in GIT_NAMES:
                record_git_call(arguments, None if session.git_environment else session.cwd, reading)
            elif name in BASH_NAMES:
                follow(name, bash_script(arguments), session.cwd, reading, depth, read_bash)


ShellReader = Callable[[str, "Path | None", Reading, int], None]
SHELL_READERS: dict[str, ShellReader] = {"Bash": read_bash, "PowerShell": read_powershell}


def read_command(tool: str, command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """Every git call a shell tool's command makes, with the directory each acts on."""
    reading = Reading(environment)
    SHELL_READERS[tool](command, cwd, reading, 0)
    return reading


def writes_checkout(call: GitCall) -> bool:
    """Whether this git call would move HEAD or write the index or tree of the checkout it runs in."""
    if call.subcommand not in WRITING_SUBCOMMANDS:
        return False
    if call.subcommand in {"checkout", "switch"}:
        return [argument for argument in call.arguments if argument not in {"-q", "--quiet"}] != [MAIN]
    if call.subcommand in {"merge", "pull"}:
        return "--ff-only" not in call.arguments
    if call.subcommand == "stash":
        return not call.arguments or call.arguments[0] not in {"list", "show"}
    if call.subcommand == "apply":
        return "--check" not in call.arguments
    return True


def denied(reading: Reading) -> bool:
    """Whether any call would write a guarded hub; a writing call whose directory is unknown becomes a note."""
    for call in reading.calls:
        if not writes_checkout(call):
            continue
        if call.directory is None:
            reading.notes.append(f"could not tell where `git {call.subcommand}` runs")
            continue
        checkout = checkout_of(call.directory)
        if checkout is not None and checkout.is_hub and guard_enabled(checkout.hub):
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------
# Subcommands


def guard(stream: str) -> int:
    try:
        event = json.loads(stream)
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        cwd = Path(platform_support.from_shell_path(event.get("cwd") or str(Path.cwd())))
        refused = False
        if tool in FILE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            refused = isinstance(target, str) and hub_protected(resolve(target, cwd))
        elif tool in SHELL_READERS and isinstance(tool_input.get("command"), str):
            reading = read_command(tool, tool_input["command"], cwd, os.environ)
            refused = denied(reading)
            for note in reading.notes:
                print(f"worktrees guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # A broken guard must not block every tool; the regression suite pins its behavior.
        print(f"worktrees guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if refused:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": DENY_REASON,
                    }
                }
            )
        )
    return 0


def current_hub() -> Path:
    checkout = checkout_of(Path.cwd())
    if checkout is None:
        raise UsageError("Run this inside the repository or one of its worktrees.")
    return checkout.hub


def new(kind: str, name: str) -> int:
    for label, value in (("kind", kind), ("name", name)):
        if not SLUG.fullmatch(value):
            raise UsageError(f"The {label} '{value}' must be lowercase letters, digits, and hyphens.")
    hub = current_hub()
    branch = f"{kind}/{name}"
    path = hub / WORKTREES / f"{kind}-{name}"
    if git(hub, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise UsageError(f"Branch {branch} already exists.")
    if path.exists():
        raise UsageError(f"{platform_support.normalize(path)} already exists.")
    if git(hub, "check-ignore", "-q", "--no-index", str(path)).returncode != 0:
        raise UsageError(f"{WORKTREES.as_posix()}/ must be ignored in the hub's .gitignore before it holds worktrees.")
    result = git(hub, "worktree", "add", "--no-track", "-b", branch, str(path), MAIN)
    if result.returncode != 0:
        raise UsageError(result.stderr.strip() or "git worktree add failed.")
    print(f"Created {platform_support.normalize(path)} on {branch} from {MAIN}.")
    print("Enter it before editing (Claude Code: EnterWorktree with this path).")
    return 0


@dataclass(frozen=True)
class Row:
    branch: str
    state: str
    ahead: str
    behind: str
    dirty: str
    path: str


def worktrees(hub: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in git(hub, "worktree", "list", "--porcelain").stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            entries.append({"worktree": value})
        elif entries and key:
            entries[-1][key] = value
    return entries


def row(hub: Path, entry: dict[str, str]) -> Row:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").removeprefix("refs/heads/") or "(detached)"
    shown = platform_support.normalize(path)
    if not path.is_dir():
        return Row(branch, "missing", "-", "-", "-", shown)
    counts = git(hub, "rev-list", "--left-right", "--count", f"{MAIN}...{entry.get('HEAD', 'HEAD')}").stdout.split()
    behind, ahead = counts if len(counts) == 2 else ("-", "-")
    dirty = len(git(path, "status", "--porcelain").stdout.splitlines())
    if same_path(path, hub):
        problems = [*(["off main"] if branch != MAIN else []), *(["dirty"] if dirty else [])]
        state = " · ".join(["hub", *problems])
    else:
        state = "no commits" if ahead == "0" else "in progress"
    return Row(branch, state, ahead, behind, str(dirty), shown)


def list_worktrees() -> int:
    hub = current_hub()
    rows = [Row("BRANCH", "STATE", "AHEAD", "BEHIND", "DIRTY", "PATH"), *(row(hub, e) for e in worktrees(hub))]
    widths = [max(len(getattr(r, field)) for r in rows) for field in ("branch", "state", "ahead", "behind", "dirty")]
    for r in rows:
        cells = (r.branch, r.state, r.ahead, r.behind, r.dirty)
        print("  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)) + "  " + r.path)
    return 0


def main(arguments: list[str]) -> int:
    platform_support.use_utf8_output()
    parser = argparse.ArgumentParser(prog="worktrees.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new", help="create a worktree for a task under .claude/worktrees")
    create.add_argument("kind", help="branch prefix, such as feat, fix, or inv")
    create.add_argument("name", help="task name")
    commands.add_parser("list", help="survey every worktree of this repository")
    commands.add_parser("guard", help="Claude Code PreToolUse hook")
    options = parser.parse_args(arguments)
    try:
        if options.command == "guard":
            return guard(sys.stdin.read())
        if options.command == "new":
            return new(options.kind, options.name)
        return list_worktrees()
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

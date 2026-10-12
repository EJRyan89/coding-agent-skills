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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from console import run_main

from deployer import platform_support

MAIN = "main"
# Where Claude Code's EnterWorktree and the desktop app create session worktrees; ignored by the hub's .gitignore.
WORKTREES = Path(".claude") / "worktrees"
GUARD_SETTING = "coding-agent-skills.hubGuard"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FILE_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
SEPARATOR_CHARACTERS = set(";&|()\n")
# Git subcommands that can move HEAD or the branch it names, or write the index or the tracked files of the
# checkout they run in, whichever subcommand or option makes them do it: the plumbing and porcelain that do, the
# interactive tools that can (difftool and mergetool, gitk, gui, citool), and the importers that commit and check
# out (archimport, cvsimport, p4, quiltimport, svn). A subcommand that writes only objects, refs other than HEAD's
# branch (fetch, notes, replace, tag), or other worktrees (worktree) is not here; git itself refuses to fetch into
# the checked-out branch without --update-head-ok, an option it reserves for git pull.
WRITING_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "archimport",
    "bisect",
    "branch",
    "checkout",
    "checkout-index",
    "cherry-pick",
    "citool",
    "clean",
    "commit",
    "cvsimport",
    "difftool",
    "fast-import",
    "filter-branch",
    "gitk",
    "gui",
    "history",
    "merge",
    "merge-file",
    "merge-index",
    "merge-one-file",
    "mergetool",
    "mv",
    "p4",
    "pull",
    "quiltimport",
    "read-tree",
    "rebase",
    "replay",
    "rerere",
    "reset",
    "restore",
    "revert",
    "rm",
    "sparse-checkout",
    "stage",
    "stash",
    "submodule",
    "svn",
    "switch",
    "symbolic-ref",
    "update-index",
    "update-ref",
}
# The only fast-forwards the hub takes: a subcommand's words besides FAST_FORWARD_OPTIONS must be one of these.
FAST_FORWARD_TARGETS = {"merge": [["origin/main"]], "pull": [[], ["origin", "main"]]}
FAST_FORWARD_OPTIONS = {"--ff-only", "-q", "--quiet", "-v", "--verbose", "--no-rebase"}
# The submodule subcommands that only read; no subcommand at all means status. foreach runs any command.
READ_SUBMODULE = {None, "status", "summary"}
# git's own options that take the next word as their value.
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--config-env", "--git-dir", "--namespace", "--super-prefix", "--work-tree"}
# The variables that choose the repository or work tree a git command writes, wherever the command runs. GIT_INDEX_FILE
# is not one: it moves only the index, never HEAD or the tree, so the directory still names the checkout written.
LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
ENVIRONMENT_DRIVE_VARIABLE = re.compile(r"^env:[\\/]?(" + "|".join(LOCATION_VARIABLES) + r")$", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GIT_NAMES = {"git"}
BASH_NAMES = {"bash", "sh"}
POWERSHELL_NAMES = {"pwsh", "powershell"}
ENV_NAMES = {"env"}
LOCATION_NAMES = {"cd", "chdir", "sl", "set-location"}
PUSH_LOCATION_NAMES = {"pushd", "push-location"}
POP_LOCATION_NAMES = {"popd", "pop-location"}
# Bash builtins that declare variables; export, and the others with -x, export them, and +x or export -n stops that.
DECLARE_NAMES = {"export", "declare", "typeset", "local", "readonly"}
# PowerShell commands that set an item, an environment variable among them when the path is on the env: drive.
SET_ITEM_NAMES = {"set-item", "si", "new-item", "ni"}
# Options of env that take no value, and those that take the next word as one.
ENV_FLAGS = {"-0", "--null", "-v", "--debug"}
ENV_UNSET = {"-u", "--unset"}
ENV_CHDIR = {"-C", "--chdir"}
ENV_CLEAR = {"-i", "--ignore-environment", "-"}
# Bash options whose next word is their value, so it is not the script that -c runs.
BASH_OPTIONS_WITH_VALUE = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
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


def git(cwd: Path, *arguments: str, environment: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Runs git on the checkout at cwd, without the location variables that would point it at another one."""
    source = os.environ if environment is None else environment
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={name: value for name, value in source.items() if name.upper() not in LOCATION_VARIABLES},
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

    Each `(` and `)` also comes out as a segment of its own, SUBSHELL_START or SUBSHELL_END, so a reader can tell
    what a subshell, `$( )`, or `<( )` confines. A quoted parenthesis standing alone reads the same; it only moves
    the scope a later directory or variable belongs to.

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
            for character in token:
                if character in "()":
                    found.extend([[character], []])
        else:
            found[-1].append(token)
    return [segment for segment in found if segment]


SUBSHELL_START = ["("]
SUBSHELL_END = [")"]


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
    return None if word is None else platform_support.program_name(Path(word).name).lower()


# The location variables a shell has set, by name; a value is None when the text does not settle it.
Variables = Mapping[str, Word]


def inherited_variables(environment: Mapping[str, str]) -> dict[str, Word]:
    """The location variables a shell starts with: those the guard's own process was given."""
    return {name: environment[name] for name in LOCATION_VARIABLES if name in environment}


def git_target(cwd: Path | None, variables: Variables) -> tuple[Path | None, bool]:
    """Where git acts from cwd under these location variables, and whether they name its work tree.

    A work tree named settles it; a repository named elsewhere without one leaves it unknown.
    """
    if "GIT_WORK_TREE" in variables:
        return moved(cwd, variables["GIT_WORK_TREE"]), True
    if "GIT_DIR" in variables or "GIT_COMMON_DIR" in variables:
        return None, False
    return cwd, False


def record_git_call(
    arguments: list[Word], directory: Path | None, reading: Reading, work_tree_named: bool = False
) -> None:
    """Reads git's own options and its subcommand: -C and --work-tree move it, --git-dir leaves it unknown.

    core.worktree set with -c names a work tree as --work-tree does, below it and GIT_WORK_TREE: an absolute path
    settles it, while a relative one, which git reads from the repository, or one taken from --config-env does not.
    """
    configured = False
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
        elif option == "--git-dir" and not work_tree_named and not configured:
            directory = None
        elif word == "-c" or option == "--config-env":
            setting = value if word == "-c" or not inline else inline_value
            key, _, configured_value = (setting or "").partition("=")
            if key.lower() == "core.worktree" and not work_tree_named:
                directory = moved(None, configured_value) if word == "-c" else None
                configured = True
        index += 2 if word in GIT_OPTIONS_WITH_VALUE else 1


def follow(
    name: str,
    script: list[Word] | None,
    cwd: Path | None,
    variables: Variables,
    reading: Reading,
    depth: int,
    read: ShellReader,
) -> None:
    """Reads the script another shell is handed as text. It runs as a process of its own, so its cd stays there.

    It starts with the variables its parent exported to it, and what it exports dies with it.
    """
    if script is None:
        return
    if not script or None in script or cwd is None or depth >= NESTING_LIMIT:
        reading.notes.append(f"could not read the command passed to {name}")
        return
    read(" ".join(word for word in script if word is not None), cwd, reading, depth + 1, variables)


def bash_script(arguments: list[Word]) -> list[Word] | None:
    """The one word bash runs from -c, alone or in a bundle such as -lc, or from a here-string; None for neither."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith(("-", "+")) or word == "--":
            break
        if not word.startswith("--") and "c" in word[1:]:
            return arguments[index + 1 : index + 2]
        index += 2 if word in BASH_OPTIONS_WITH_VALUE else 1
    if arguments[index : index + 1] == ["<<<"]:
        return arguments[index + 1 : index + 2]
    return None


def powershell_script(arguments: list[Word]) -> list[Word] | None:
    """Every word after pwsh's -Command, or an abbreviation of it; None when it runs no inline script."""
    for index, word in enumerate(arguments):
        if word is not None and POWERSHELL_COMMAND_SWITCH.fullmatch(word):
            return arguments[index + 1 :]
    return None


@dataclass
class Shell:
    """A Bash shell as the guard follows it: its directory, its pushd stack, and the location variables it holds.

    exported holds the variables its commands see; unexported, those it set without exporting, which a later
    `export NAME`, or an allexport in force, hands on.
    """

    cwd: Path | None
    exported: dict[str, Word]
    unexported: dict[str, Word] = field(default_factory=dict)
    stack: list[Path | None] = field(default_factory=list)
    allexport: bool = False

    def subshell(self) -> Shell:
        return Shell(self.cwd, dict(self.exported), dict(self.unexported), list(self.stack), self.allexport)

    def assign(self, name: str, value: Word, export: bool) -> None:
        """NAME=value: a variable already exported stays so, as every variable does under allexport."""
        if name not in LOCATION_VARIABLES:
            return
        if export or self.allexport or name in self.exported:
            self.exported[name] = value
            self.unexported.pop(name, None)
        else:
            self.unexported[name] = value

    def export(self, name: str) -> None:
        if name in self.unexported:
            self.exported[name] = self.unexported.pop(name)

    def unexport(self, name: str) -> None:
        if name in self.exported:
            self.unexported[name] = self.exported.pop(name)

    def unset(self, name: str) -> None:
        self.exported.pop(name, None)
        self.unexported.pop(name, None)


def read_bash(command: str, cwd: Path | None, reading: Reading, depth: int, variables: Variables) -> None:
    """Reads a Bash command one simple command at a time; what a subshell changes stays inside it."""
    allexport = "allexport" in reading.environment.get("SHELLOPTS", "").split(":")
    shell = Shell(cwd, dict(variables), allexport=allexport)
    outer: list[Shell] = []
    for segment in segments(command):
        if segment == SUBSHELL_START:
            outer.append(shell)
            shell = shell.subshell()
        elif segment == SUBSHELL_END:
            shell = outer.pop() if outer else shell
        else:
            read_bash_command(segment, shell, reading, depth)


def read_bash_command(segment: list[str], shell: Shell, reading: Reading, depth: int) -> None:
    """One simple command: its own assignments, then a builtin that changes the shell or a program it starts."""
    index = 0
    local: dict[str, Word] = {}
    while index < len(segment) and ASSIGNMENT.match(segment[index]):
        name, _, value = segment[index].partition("=")
        if name in LOCATION_VARIABLES:
            local[name] = value
        index += 1
    if index >= len(segment):
        for variable, assigned in local.items():
            shell.assign(variable, assigned, export=False)
        return
    name, words = segment[index], segment[index + 1 :]
    if name == "cd":
        shell.cwd = bash_cd(shell.cwd, words)
    elif name == "pushd":
        shell.stack.append(shell.cwd)
        target = words[0] if len(words) == 1 and not words[0].startswith(("-", "+")) else None
        shell.cwd = None if target is None else location_after(shell.cwd, True, target)
    elif name == "popd":
        shell.cwd = shell.stack.pop() if shell.stack else None
    elif name in DECLARE_NAMES:
        read_declaration(shell, name, words)
    elif name == "unset":
        if "-f" not in words:
            for word in words:
                shell.unset(word)
    elif name == "set":
        read_set(shell, words)
    else:
        run_program(command_name(name), list(words), shell.cwd, {**shell.exported, **local}, reading, depth)


def bash_cd(cwd: Path | None, words: list[str]) -> Path | None:
    """Where Bash's cd leaves the shell: home when bare, unknown for `cd -`; with two paths it fails and stays."""
    paths = [word for word in words if word not in {"-L", "-P", "-e", "-@"}]
    if len(paths) > 1:
        return cwd
    return location_after(cwd, bool(paths), paths[0] if paths else None)


def read_declaration(shell: Shell, builtin: str, words: list[str]) -> None:
    """export, declare, typeset, local, and readonly: which location variables they set, export, or stop exporting."""
    exporting, unexporting = builtin == "export", False
    for word in words:
        if word.startswith(("-", "+")) and not ASSIGNMENT.match(word):
            flags = word[1:]
            if "f" in flags:
                return  # Functions, not variables.
            if builtin == "export" and "n" in flags:
                unexporting = True
            elif "x" in flags:
                exporting, unexporting = word.startswith("-"), word.startswith("+")
            continue
        name, has_value, value = word.partition("=")
        if unexporting:
            shell.unexport(name)
            if has_value:
                shell.assign(name, value, export=False)
        elif has_value:
            shell.assign(name, value, export=exporting)
        elif exporting:
            shell.export(name)


def read_set(shell: Shell, words: list[str]) -> None:
    """set -a, set -o allexport, and their + forms, which export every variable assigned while they hold."""
    index = 0
    while index < len(words):
        word = words[index]
        if word in {"-o", "+o"}:
            if words[index + 1 : index + 2] == ["allexport"]:
                shell.allexport = word == "-o"
            index += 2
            continue
        if word == "--" or len(word) < 2 or word[0] not in "-+" or not word[1:].isalpha():
            return
        if "a" in word[1:]:
            shell.allexport = word[0] == "-"
        index += 1


def run_program(
    program: str | None, arguments: list[Word], cwd: Path | None, variables: Variables, reading: Reading, depth: int
) -> None:
    """A program a Bash command starts, in cwd and with these location variables in its environment."""
    if program in GIT_NAMES:
        directory, work_tree_named = git_target(cwd, variables)
        record_git_call(arguments, directory, reading, work_tree_named)
    elif program in BASH_NAMES:
        follow(program, bash_script(arguments), cwd, variables, reading, depth, read_bash)
    elif program in POWERSHELL_NAMES:
        follow(program, powershell_script(arguments), cwd, variables, reading, depth, read_powershell)
    elif program in ENV_NAMES:
        read_env(arguments, cwd, variables, reading, depth)


def read_env(arguments: list[Word], cwd: Path | None, variables: Variables, reading: Reading, depth: int) -> None:
    """env starts its program with the environment it is given: emptied by -i, less each -u, plus each NAME=value."""
    given = dict(variables)
    index = 0
    while index < len(arguments):
        word = arguments[index]
        value = arguments[index + 1] if index + 1 < len(arguments) else None
        if word is None or not word.startswith("-") or word == "--":
            index += word == "--"
            break
        option, _, inline_value = word.partition("=")
        if word in ENV_CLEAR:
            given.clear()
        elif word in ENV_UNSET or option == "--unset" or (word.startswith("-u") and not word.startswith("--")):
            given.pop((value if word in ENV_UNSET else inline_value if option == "--unset" else word[2:]) or "", None)
        elif word in ENV_CHDIR or option == "--chdir":
            cwd = moved(cwd, value if word in ENV_CHDIR else inline_value)
        elif word not in ENV_FLAGS:
            reading.notes.append(f"could not read the option {word} passed to env")
            return
        index += 2 if word in ENV_UNSET | ENV_CHDIR else 1
    while index < len(arguments) and ASSIGNMENT.match(arguments[index] or ""):
        name, _, assigned = (arguments[index] or "").partition("=")
        if name in LOCATION_VARIABLES:
            given[name] = assigned
        index += 1
    if index < len(arguments):
        run_program(command_name(arguments[index]), arguments[index + 1 :], cwd, given, reading, depth)


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


def mentions_alias(command: str, cwd: Path | None, environment: Mapping[str, str]) -> bool:
    """Whether the text names an alias git defines where the command starts; true when they cannot be listed."""
    if cwd is None:
        return True
    result = git(cwd, "config", "--name-only", "--get-regexp", r"^alias\.", environment=environment)
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        return True
    names = (line.partition(".")[2] for line in result.stdout.splitlines())
    return any(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", command, re.IGNORECASE) for name in names if name)


def may_refuse(command: str, cwd: Path | None, environment: Mapping[str, str]) -> bool:
    """Whether the text names git and, as a word, a refused subcommand or an alias: what pwsh is started to read."""
    if not MENTIONS_GIT.search(command):
        return False
    return bool(MENTIONS_WRITING_SUBCOMMAND.search(command)) or mentions_alias(command, cwd, environment)


@dataclass
class Session:
    """A PowerShell session's location, its Push-Location stack, and the location variables it holds.

    A variable the command assigns is held with no value: the guard does not follow what PowerShell computes.
    """

    cwd: Path | None
    variables: dict[str, Word]
    stack: list[Path | None] = field(default_factory=list)


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int, variables: Variables) -> None:
    """Reads a PowerShell command with PowerShell's own parser, started only for text it could refuse.

    Starting pwsh costs far more than the guard's git calls, so text that does not name git and either a refused
    subcommand or one of git's aliases never pays for it. A location belongs to the session, not to a script
    block's scope, so only a child pwsh, which the reader brackets with push and pop, restores one.
    """
    if not may_refuse(command, cwd, reading.environment):
        return
    events = powershell_events(command, reading)
    if events is None:
        return
    parents: list[Session] = []
    session = Session(cwd, dict(variables))
    for event in events:
        kind = event.get("kind")
        if kind == "push":
            parents.append(session)
            session = Session(session.cwd, dict(session.variables))
        elif kind == "pop":
            session = parents.pop() if parents else session
        elif kind == "gitEnv":
            session.variables[str(event.get("name", "GIT_DIR")).upper()] = None
        elif kind == "note":
            reading.notes.append(str(event.get("text")))
        elif kind == "command":
            words: list[Word] = [None if word["dynamic"] else word["text"] for word in event["words"]]
            read_powershell_command(words, session, reading, depth)


def read_powershell_command(words: list[Word], session: Session, reading: Reading, depth: int) -> None:
    """One PowerShell command: a location change, a location variable set on the env: drive, or a program."""
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
        directory, work_tree_named = git_target(session.cwd, session.variables)
        record_git_call(arguments, directory, reading, work_tree_named)
    elif name in BASH_NAMES:
        follow(name, bash_script(arguments), session.cwd, session.variables, reading, depth, read_bash)
    elif name in SET_ITEM_NAMES:
        for word in arguments:
            found = ENVIRONMENT_DRIVE_VARIABLE.match(word or "")
            if found:
                session.variables[found.group(1).upper()] = None


ShellReader = Callable[[str, "Path | None", Reading, int, Variables], None]
SHELL_READERS: dict[str, ShellReader] = {"Bash": read_bash, "PowerShell": read_powershell}


def read_command(tool: str, command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """Every git call a shell tool's command makes, with the directory each acts on."""
    reading = Reading(environment)
    SHELL_READERS[tool](command, cwd, reading, 0, inherited_variables(environment))
    return reading


def allowed_form(subcommand: str, arguments: list[str]) -> bool:
    """Whether a refused subcommand is called in one of the forms that leave the hub on main and its tree alone."""
    if subcommand in {"checkout", "switch"}:
        return [argument for argument in arguments if argument not in {"-q", "--quiet"}] == [MAIN]
    if subcommand == "stash":
        return bool(arguments) and arguments[0] in {"list", "show"}
    if subcommand == "apply":
        return "--check" in arguments
    if subcommand == "submodule":
        return next((argument for argument in arguments if not argument.startswith("-")), None) in READ_SUBMODULE
    if subcommand == "branch":
        return not renames_a_branch(arguments)
    return False


def renames_a_branch(arguments: list[str]) -> bool:
    """Whether git branch is asked to move a branch, which renames HEAD's own when it names none or that one.

    git takes a unique prefix of a long option and a bundle of short ones, so --mov and -fm both move.
    A short option's attached value ends its bundle; -u also takes a separate value, while -t does not.
    """
    options = iter(arguments)
    for argument in options:
        if argument == "--":
            return False
        if argument.startswith("--"):
            name, inline, _ = argument[2:].partition("=")
            if len(name) >= 2 and "move".startswith(name):
                return True
            # Shorter prefixes also match git's hidden, valueless --set-upstream option.
            if not inline and name.startswith("set-upstream-") and "set-upstream-to".startswith(name):
                next(options, None)
        elif argument.startswith("-"):
            for index, option in enumerate(argument[1:], start=1):
                if option in {"m", "M"}:
                    return True
                if option in {"u", "t"}:
                    if option == "u" and index == len(argument) - 1:
                        next(options, None)
                    break
    return False


def refusal(call: GitCall) -> str | None:
    """Why this git call would move HEAD or write the index or tree of the checkout it runs in; None if it would not."""
    subcommand, arguments = call.subcommand, call.arguments
    if subcommand not in WRITING_SUBCOMMANDS or allowed_form(subcommand, arguments):
        return None
    if subcommand in {"merge", "pull"} and "--ff-only" in arguments:
        target = [argument for argument in arguments if argument not in FAST_FORWARD_OPTIONS]
        if target in FAST_FORWARD_TARGETS[subcommand]:
            return None
        shown = " ".join(target) or "its upstream"
        return f"`git {subcommand} --ff-only` moves the hub only to origin/main, not to {shown}."
    return f"`git {subcommand}` would move the hub's HEAD or write its index or tree."


def alias_refusal(call: GitCall, hub: Path, environment: Mapping[str, str], depth: int) -> str | None:
    """Why the alias a call names would write the hub; None when it is no alias, or one that would not.

    git runs a builtin before an alias, so only a subcommand the guard does not judge itself is looked up. An
    alias that cannot be read is refused, unlike a command whose text cannot be: it hides its subcommand.
    """
    result = git(hub, "config", "--get", f"alias.{call.subcommand}")
    if result.returncode == 1:
        return None
    expansion = result.stdout.removesuffix("\n")
    unreadable = f"The alias `git {call.subcommand}` could not be read"
    if result.returncode != 0:
        return f"{unreadable} ({result.stderr.strip()}), so it is refused."
    if depth >= NESTING_LIMIT:
        return f"{unreadable} (it nests more than {NESTING_LIMIT} deep), so it is refused."
    reading = Reading(environment)
    if expansion.startswith("!"):
        # git runs a shell alias at the top of the checkout, with the call's arguments after it.
        script = " ".join([expansion[1:], *map(shlex.quote, call.arguments)])
        read_bash(script, hub, reading, depth + 1, inherited_variables(environment))
    else:
        try:
            words = shlex.split(expansion)
        except ValueError as exc:
            return f"{unreadable} ({exc}), so it is refused."
        record_git_call([*words, *call.arguments], call.directory, reading)
    reason = denied(reading, depth + 1)
    if reason is not None:
        return f"`git {call.subcommand}` is an alias for `{expansion}`: {reason}"
    if reading.notes:
        return f"{unreadable} ({reading.notes[0]}), so it is refused."
    return None


def denied(reading: Reading, depth: int = 0) -> str | None:
    """Why a call would write a guarded hub, or None; a refused call whose directory is unknown becomes a note.

    A call that is not a refused subcommand may be an alias, which is looked up in the hub it runs in.
    """
    hubs: dict[Path, Path | None] = {}
    for call in reading.calls:
        writing = call.subcommand in WRITING_SUBCOMMANDS
        if writing and refusal(call) is None:
            continue
        if call.directory is None:
            if writing:
                reading.notes.append(f"could not tell where `git {call.subcommand}` runs")
            continue
        if call.directory not in hubs:
            checkout = checkout_of(call.directory)
            guarded = checkout is not None and checkout.is_hub and guard_enabled(checkout.hub)
            hubs[call.directory] = checkout.hub if checkout is not None and guarded else None
        hub = hubs[call.directory]
        if hub is None:
            continue
        reason = refusal(call) if writing else alias_refusal(call, hub, reading.environment, depth)
        if reason is not None:
            return reason
    return None


# ---------------------------------------------------------------------------------------------------------------
# Subcommands


def guard(stream: str) -> int:
    try:
        event = json.loads(stream)
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        cwd = Path(platform_support.from_shell_path(event.get("cwd") or str(Path.cwd())))
        refused: str | None = None
        if tool in FILE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(target, str) and hub_protected(resolve(target, cwd)):
                refused = DENY_REASON
        elif tool in SHELL_READERS and isinstance(tool_input.get("command"), str):
            reading = read_command(tool, tool_input["command"], cwd, os.environ)
            reason = denied(reading)
            refused = None if reason is None else f"{reason} {DENY_REASON}"
            for note in reading.notes:
                print(f"worktrees guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # A broken guard must not block every tool; the regression suite pins its behavior.
        print(f"worktrees guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if refused is not None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": refused,
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
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")

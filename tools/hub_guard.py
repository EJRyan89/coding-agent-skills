"""The hub guard: the Claude Code PreToolUse hook .claude/settings.json runs, which reads the hook JSON on stdin.

Usage:
  python tools/hub_guard.py   reads one PreToolUse event on stdin and prints a deny decision, or nothing

The guard refuses file edits inside the hub and git commands that would move the hub's HEAD or write its tree,
whether they run through the Bash tool, the PowerShell tool, or the Monitor tool, whose command runs in Bash. Each
command is read by its shell's own grammar: Bash by tools/bash_syntax.py, PowerShell by PowerShell's own parser
(tools/hub_guard-powershell.ps1). What a reader settles is judged; a git call it cannot place is refused while a
guarded hub is in view; what it does not see is named in docs/parallel-sessions.md ("The guard's contract"). Every
decision is made here. It finds the checkout that holds a path, and runs git, through tools/worktrees.py, which owns
the hub and its worktrees. It is inert unless `git config coding-agent-skills.hubGuard true` was set in this clone.
"""

from __future__ import annotations

import base64
import fnmatch
import functools
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded
from console import run_main
from git_client import GLOBAL_VALUE_OPTIONS as GIT_OPTIONS_WITH_VALUE
from git_client import GitError
from job_tree import process_tree

from deployer import platform_support
from tools import bash_syntax
from tools.worktrees import MAIN, checkout_of, git, same_path

GUARD_SETTING = "coding-agent-skills.hubGuard"
FILE_TOOLS = {"Edit", "NotebookEdit", "Write"}
# Git subcommands that can move HEAD or the branch it names, or write the index or the tracked files of the
# checkout they run in, whichever subcommand or option makes them do it: the plumbing and porcelain that do, the
# interactive tools that can (difftool and mergetool, gui, citool), and the importers that commit and check
# out (archimport, cvsimport, p4, quiltimport, svn). A subcommand that writes only objects, refs other than HEAD's
# branch (notes, replace, tag), or other worktrees (worktree) is not here. fetch is judged by its options: with
# --update-head-ok it moves the branch HEAD names, as git pull asks it to.
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
FETCH = "fetch"
# The only fast-forwards the hub takes: a subcommand's words besides FAST_FORWARD_OPTIONS must be one of these.
FAST_FORWARD_TARGETS = {"merge": [["origin/main"]], "pull": [[], ["origin", "main"]]}
FAST_FORWARD_OPTIONS = {"--ff-only", "-q", "--quiet", "-v", "--verbose", "--no-rebase"}
# The submodule subcommands that only read; no subcommand at all means status. foreach runs any command.
READ_SUBMODULE = {None, "status", "summary"}
# git's own options, read as git.c reads them: no abbreviation, and a value either attached after = or the next word.
# Those that take a value are git_client's GLOBAL_VALUE_OPTIONS, imported above as GIT_OPTIONS_WITH_VALUE.
GIT_OPTIONS_WITHOUT_VALUE = {
    "-p",
    "--paginate",
    "-P",
    "--no-pager",
    "--bare",
    "--no-replace-objects",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-advice",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
}
# Options after which git prints something and runs no subcommand of the command's.
GIT_OPTIONS_THAT_PRINT = {"--version", "-v", "--help", "-h", "--exec-path", "--html-path", "--man-path", "--info-path"}
# The variables that choose the repository or work tree whose HEAD, index, and files a git command writes.
# GIT_COMMON_DIR is not one: it moves only the refs and objects every worktree shares, never a checkout's HEAD, index,
# or tree. Nor is GIT_INDEX_FILE, which moves only the index; the guard judges such a call by the checkout it runs in.
LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE")
# The variables that carry command-line configuration, which git reads for core.worktree and alias.<name>.
CONFIGURATION_VARIABLES = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
CONFIGURATION_PAIR = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+")
# A path on the environment provider: on the env: drive, or provider-qualified as Environment::, which may carry a
# leading backslash or, without one, the provider's module. A variable may follow a separator on the drive only: after
# Environment:: PowerShell keeps the separator as part of the variable's name.
ENVIRONMENT_QUALIFIED = r"(?:\\|Microsoft\.PowerShell\.Core\\)?Environment::"
ENVIRONMENT_PROVIDER = r"^(?:env:|" + ENVIRONMENT_QUALIFIED + r")"
ENVIRONMENT_ITEM = re.compile(r"^(?:env:[\\/]?|" + ENVIRONMENT_QUALIFIED + r")([^\\/:]+)$", re.IGNORECASE)
ENVIRONMENT_ROOT = re.compile(ENVIRONMENT_PROVIDER + r"[\\/]?$", re.IGNORECASE)
ENVIRONMENT_PATH = re.compile(ENVIRONMENT_PROVIDER, re.IGNORECASE)
# A PowerShell location on a drive or provider that is not a drive letter, such as env:, HKLM:, or Environment::.
PROVIDER_LOCATION = re.compile(r"^(?:(?:[\w.]+\\)?[A-Za-z]\w*::|[A-Za-z][\w-]+:)")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=")
GIT_NAMES = {"git"}
BASH_NAMES = {"bash", "sh"}
POWERSHELL_NAMES = {"pwsh", "powershell"}
ENV_NAMES = {"env"}
LOCATION_NAMES = {"cd", "chdir", "sl", "set-location"}
PUSH_LOCATION_NAMES = {"pushd", "push-location"}
POP_LOCATION_NAMES = {"popd", "pop-location"}
# Bash builtins that declare variables; export, and the others with -x, export them, and +x or export -n stops that.
DECLARE_NAMES = {"export", "declare", "typeset", "local", "readonly"}
# PowerShell commands that set an environment variable when a path they write is on the environment provider, and
# the parameters, as the command declares them, that name that path. Add-Content and Move-Item fail there: the
# provider can neither seek within a variable nor move one.
ENVIRONMENT_WRITERS = {
    **dict.fromkeys(["set-item", "si", "new-item", "ni", "set-content", "sc"], ("path", "literalpath")),
    **dict.fromkeys(["copy-item", "copy", "cp", "cpi"], ("destination",)),
}
# Commands that remove the variable their path names on the environment provider.
ENVIRONMENT_REMOVERS = {"remove-item", "ri", "rm", "rmdir", "del", "erase", "rd", "clear-item", "cli"}
# New-Item names the variable with -Name when its path is the provider's root; Rename-Item names it with -NewName.
NEW_ITEM_NAMES = {"new-item", "ni"}
RENAME_ITEM_NAMES = {"rename-item", "ren", "rni"}
NEW_DRIVE_NAMES = {"new-psdrive", "ndr", "mount"}
# Options of env that take no value, and those that take the next word as one.
ENV_FLAGS = {"-0", "--null", "-v", "--debug"}
ENV_UNSET = {"-u", "--unset"}
ENV_CHDIR = {"-C", "--chdir"}
ENV_CLEAR = {"-i", "--ignore-environment", "-"}
# Programs that start the command after their own options: each one's options that take no value, those that take
# the next word, and how many operands come before the command.
PREFIX_PROGRAMS: dict[str, tuple[frozenset[str], frozenset[str], int]] = {
    "nohup": (frozenset(), frozenset(), 0),
    "nice": (frozenset(), frozenset({"-n", "--adjustment"}), 0),
    "timeout": (
        frozenset({"-v", "--verbose", "--preserve-status", "--foreground"}),
        frozenset({"-s", "--signal", "-k", "--kill-after"}),
        1,
    ),
    "stdbuf": (frozenset(), frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    # The program, which Bash runs where `time` is not a command's first word, as after an assignment or a
    # redirection, or is quoted.
    "time": (
        frozenset({"-p", "--portability", "-v", "--verbose", "-a", "--append", "-q", "--quiet"}),
        frozenset({"-f", "--format", "-o", "--output"}),
        0,
    ),
}
# Bash options whose next word is their value, so it is not the script that -c runs.
BASH_OPTIONS_WITH_VALUE = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
POWERSHELL_COMMAND_SWITCH = re.compile(r"-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?", re.IGNORECASE)
POWERSHELL_COMMAND_WITH_ARGUMENTS_SWITCH = re.compile(r"-cwa|-commandw(?:i(?:t(?:h(?:a(?:r(?:g(?:s)?)?)?)?)?)?)?", re.I)
# How deep bash -c, pwsh -Command, eval, a function call, and Invoke-Expression are followed; the PowerShell reader is
# handed what is left of it. Deeper nesting is a command the guard cannot read.
NESTING_LIMIT = 4
POWERSHELL_READER = Path(__file__).with_name("hub_guard-powershell.ps1")
# Bounds only a pwsh that hangs: reading is linear in the command. Generous, since a command pwsh does not read in time
# is refused while a hub is guarded, and well inside the hook's own 30-second timeout in .claude/settings.json.
POWERSHELL_TIMEOUT_SECONDS = 20
# How much of the reader's output is kept: far more than the events of any command a tool call carries, and a reading
# cut short is not JSON, so it is a command the guard cannot read rather than one read in part.
POWERSHELL_OUTPUT_LIMIT = 1 << 22
MENTIONS_GIT = re.compile(r"git", re.IGNORECASE)
MENTIONS_WRITING_SUBCOMMAND = re.compile(
    r"\b(?:" + "|".join(map(re.escape, sorted(WRITING_SUBCOMMANDS | {FETCH}))) + r")\b", re.IGNORECASE
)
# A git word followed, in the same statement, by something that computes a word: what a subcommand could be made of.
COMPUTED_AFTER_GIT = re.compile(r"git[^;|&\n]*[$@(`{\[]", re.IGNORECASE)
# The characters either shell joins one word across without a space: Bash's quotes and backslash, and PowerShell's
# quotes, its typographic quotes, and its backtick, so com'mit' is the word commit to both.
WORD_JOINERS = re.compile(r"[\"'`\\\u2018\u2019\u201a\u201b\u201c\u201d\u201e]")
DENY_REASON = (
    "This checkout is the hub: it stays on main and is never edited, so parallel sessions cannot carry each "
    "other's changes (docs/parallel-sessions.md). Create a worktree with "
    "`python tools/worktrees.py new <kind> <name>`, enter it, and work there."
)
PLACE_IT = "Write the directory into the command itself, as `git -C <dir>` or `cd <dir> &&`, and run it again."


def same_place(left: Path | None, right: Path | None) -> bool:
    return left is right or (left is not None and right is not None and same_path(left, right))


def guard_enabled(hub: Path) -> bool:
    """Reads the opt-in from the repository's own configuration file only: --local passes over command-line
    configuration, which GIT_CONFIG_PARAMETERS and GIT_CONFIG_COUNT carry and which would otherwise outrank it. git
    exits 1 for a setting that is not there; any other failure, such as a value that is no boolean, is a hub it cannot
    look up, allowed with a note rather than read as unguarded unseen."""
    result = git(hub, "config", "--local", "--type=bool", "--get", GUARD_SETTING)
    if result.returncode not in (0, 1):
        said = " ".join(result.stderr.split()) or f"git config exited with {result.returncode}"
        print(f"hub guard skipped: the opt-in of {hub} could not be read: {said}", file=sys.stderr)
    return result.returncode == 0 and result.stdout.strip() == "true"


@functools.cache
def guarded_hub_at(path: Path) -> Path | None:
    """The guarded hub whose checkout holds path; None when it is a worktree, another repository, or none."""
    checkout = checkout_of(path)
    if checkout is None or not checkout.is_hub or not guard_enabled(checkout.hub):
        return None
    return checkout.hub


@functools.cache
def guarded_hub_of_repository(path: Path) -> Path | None:
    """The guarded hub whose own git directory path names, as --git-dir or GIT_DIR does; None for a worktree's, another
    repository's, or a path that names no repository, where git runs nothing."""
    directory = path if path.is_dir() else path.parent
    if not directory.is_dir():
        return None
    result = git(
        directory, "--git-dir", str(path), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2 or not same_path(Path(lines[0]), Path(lines[1])):
        return None
    common = Path(lines[1])
    if os.path.normcase(common.name) != ".git":
        return None
    hub = guarded_hub_at(common.parent)
    return hub if hub is not None and same_path(hub, common.parent) else None


def resolve(value: str, cwd: Path) -> Path:
    path = Path(platform_support.from_shell_path(value))
    return path if path.is_absolute() else cwd / path


@functools.cache
def git_builtins() -> frozenset[str]:
    """The subcommands git runs itself, which no alias can stand for; empty when git cannot list them."""
    result = git(Path(__file__).resolve().parent, "--list-cmds=builtins")
    return frozenset(result.stdout.split()) if result.returncode == 0 else frozenset()


def hub_protected(path: Path) -> bool:
    """Whether path lies in the tracked surface of a guarded hub or anywhere in its git directory; ignored files such
    as local settings do not."""
    normalized = Path(os.path.normpath(path))
    for directory in (normalized, *normalized.parents):
        if os.path.normcase(directory.name) == ".git" and directory.is_dir():
            return guarded_hub_of_repository(directory) is not None
    hub = guarded_hub_at(normalized)
    if hub is None:
        return False
    return git(hub, "check-ignore", "-q", "--no-index", str(normalized)).returncode != 0


def hubs_in_view(cwd: Path, environment: Mapping[str, str]) -> set[Path]:
    """The guarded hubs a session can reach: the hub of its own directory's checkout, a worktree's included, and of
    its project's. A directory whose checkout git cannot read is noted and passed over, so the others still count."""
    directories = [cwd]
    project = environment.get("CLAUDE_PROJECT_DIR")
    if project:
        directories.append(Path(platform_support.from_shell_path(project)))
    found: set[Path] = set()
    for directory in directories:
        try:
            checkout = checkout_of(directory)
        except GitError as exc:
            print(f"hub guard could not look up the hub of {directory}: {exc}", file=sys.stderr)
            continue
        if checkout is not None and guard_enabled(checkout.hub):
            found.add(checkout.hub)
    return found


# ---------------------------------------------------------------------------------------------------------------
# What the readers produce


@dataclass(frozen=True)
class Place:
    """Where one git call acts, as far as the text settles it.

    start is where git starts, after each -C; tree is the work tree whose files it writes; repository is the git
    directory whose HEAD and index it writes when one is named, and otherwise git finds it from start. None is a
    place the text does not settle. aliases holds the alias.<name> settings given on the command line, or is None when
    command-line configuration the text does not settle may hold any.
    """

    start: Path | None
    tree: Path | None
    repository: Path | None = None
    repository_named: bool = False
    aliases: Mapping[str, str | None] | None = field(default_factory=dict)


@dataclass(frozen=True)
class GitCall:
    place: Place
    subcommand: str | None  # None when the text does not settle it
    arguments: list[str]
    # git's own options as read before the subcommand, from which an alias's expansion goes on reading.
    options: GitOptions | None = None

    @property
    def directory(self) -> Path | None:
        return self.place.tree


@dataclass
class Reading:
    """What the guard read from one command: the git calls it makes, in order, the parts of it the guard could not
    read, which are refused while a guarded hub is in view, and notes on what it passed over."""

    environment: Mapping[str, str]
    text: str = ""
    calls: list[GitCall] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    context_hubs: set[Path] = field(default_factory=set)

    def cannot_read(self, what: str) -> None:
        """Text that may start git and that the guard cannot read: refused when the command names git at all."""
        (self.unread if mentions(MENTIONS_GIT, self.text) else self.notes).append(what)


def mentions(pattern: re.Pattern[str], text: str) -> bool:
    """Whether pattern finds a word in the text as written, or with the quotes and escapes that join a word taken out
    as either shell takes them out."""
    return any(pattern.search(spelling) for spelling in (text, WORD_JOINERS.sub("", text)))


# A word is its text, or None when the text does not settle its value (an unset variable, a $( ) result).
Word = str | None
# The variables a shell has set, by name; a value is None when the text does not settle it.
Variables = Mapping[str, Word]


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


def folded(name: str) -> str:
    """The name git reads a variable by: in any case on Windows, where git takes git_dir as GIT_DIR, and only as
    written elsewhere."""
    return name.upper() if platform_support.ENVIRONMENT_NAMES_FOLD_CASE else name


def is_tracked(name: str) -> bool:
    """Whether git reads this variable to choose where it acts or which aliases it has."""
    canonical = folded(name)
    return (
        canonical in LOCATION_VARIABLES
        or canonical in CONFIGURATION_VARIABLES
        or bool(CONFIGURATION_PAIR.fullmatch(canonical))
    )


def tracked_variables(variables: Variables) -> dict[str, Word]:
    return {name: value for name, value in variables.items() if is_tracked(name)}


def git_variables(variables: Variables, notes: list[str]) -> dict[str, Word]:
    """The tracked variables git reads, by the name git reads them by.

    On Windows two spellings of one name can both reach git; git takes the upper-case one, so a conflict is judged by
    it, with a note, and when no upper-case spelling is among them their value is not settled.
    """
    spellings: dict[str, dict[str, Word]] = {}
    for name, value in tracked_variables(variables).items():
        spellings.setdefault(folded(name), {})[name] = value
    values: dict[str, Word] = {}
    for canonical, found in spellings.items():
        distinct = set(found.values())
        if len(distinct) == 1:
            values[canonical] = distinct.pop()
        elif canonical in found:
            values[canonical] = found[canonical]
            others = ", ".join(sorted(name for name in found if name != canonical))
            notes.append(
                f"{canonical} and {others} name different values; git reads {canonical}, so it was judged by it"
            )
        else:
            values[canonical] = None
    return values


def inherited_variables(environment: Mapping[str, str]) -> dict[str, Word]:
    """The tracked variables a shell starts with: those the guard's own process was given."""
    return dict(tracked_variables(environment))


# ---------------------------------------------------------------------------------------------------------------
# The git reader


@dataclass
class Configuration:
    """Command-line configuration a git call sees: GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT and its pairs, then each -c
    and --config-env, the later winning. settled is False once an entry's key is not settled by the text."""

    entries: list[tuple[str, Word]] = field(default_factory=list)
    settled: bool = True

    def add(self, setting: Word) -> None:
        if setting is None:
            self.settled = False
            return
        key, has_value, value = setting.partition("=")
        self.entries.append((key.lower(), value if has_value else "true"))

    def value(self, key: str) -> tuple[bool, Word]:
        """Whether key is set on the command line, and its value; a configuration not settled may set any key."""
        found = [value for name, value in self.entries if name == key]
        if found:
            return True, found[-1]
        return (False, None) if self.settled else (True, None)

    def aliases(self) -> dict[str, str | None] | None:
        if not self.settled:
            return None
        return {name.removeprefix("alias."): value for name, value in self.entries if name.startswith("alias.")}


def environment_configuration(values: Mapping[str, Word]) -> Configuration:
    """The command-line configuration git's environment carries, in the order git reads it."""
    configuration = Configuration()
    if "GIT_CONFIG_PARAMETERS" in values:
        parameters = values["GIT_CONFIG_PARAMETERS"]
        try:
            settings = shlex.split(parameters) if parameters is not None else None
        except ValueError:
            settings = None
        for setting in settings if settings is not None else [None]:
            configuration.add(setting)
    if "GIT_CONFIG_COUNT" in values:
        count = values["GIT_CONFIG_COUNT"]
        if count is None or not count.strip().isdigit():
            configuration.settled = False
            return configuration
        for index in range(int(count)):
            key, value = values.get(f"GIT_CONFIG_KEY_{index}"), values.get(f"GIT_CONFIG_VALUE_{index}")
            configuration.add(None if key is None or value is None else f"{key}={value}")
    for key, _ in configuration.entries:
        if key.startswith(("include.", "includeif.")):
            configuration.settled = False
    return configuration


@dataclass
class GitOptions:
    """git's own options as read so far: where it starts, the git directory and work tree named, and configuration."""

    start: Path | None
    git_dir: tuple[bool, Word]
    work_tree: tuple[bool, Word]
    configuration: Configuration

    @classmethod
    def initial(cls, cwd: Path | None, values: Mapping[str, Word], opaque: bool) -> GitOptions:
        if opaque:
            # Variables the text does not show may name any repository or work tree.
            return cls(cwd, (True, None), (True, None), Configuration(settled=False))
        return cls(
            cwd,
            ("GIT_DIR" in values, values.get("GIT_DIR")),
            ("GIT_WORK_TREE" in values, values.get("GIT_WORK_TREE")),
            environment_configuration(values),
        )

    def copy(self) -> GitOptions:
        return replace(self, configuration=replace(self.configuration, entries=list(self.configuration.entries)))

    def place(self) -> Place:
        """Relative paths are read from where git finally starts, as git reads them once every -C has run."""
        named_dir, git_dir = self.git_dir
        repository = (
            None if not named_dir or (self.start is None and not is_absolute(git_dir)) else moved(self.start, git_dir)
        )
        named_tree, work_tree = self.work_tree
        configured, core_worktree = self.configuration.value("core.worktree")
        if named_tree:
            tree = moved(self.start, work_tree)
        elif configured:
            # A relative core.worktree is read from the git directory, which only a named one settles.
            base = repository if named_dir else None
            tree = moved(base, core_worktree)
        else:
            tree = self.start
        return Place(self.start, tree, repository, named_dir, self.configuration.aliases())


def is_absolute(value: Word) -> bool:
    return value is not None and Path(platform_support.from_shell_path(value)).is_absolute()


def read_git_options(arguments: list[Word], options: GitOptions, reading: Reading) -> int | None:
    """Reads git's own options into options; returns the index of the subcommand, or None when git runs none."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith("-"):
            return index
        option, inline, inline_value = word.partition("=")
        separate = option in GIT_OPTIONS_WITH_VALUE and not inline
        value = inline_value if inline else (arguments[index + 1] if index + 1 < len(arguments) else None)
        if word in GIT_OPTIONS_THAT_PRINT:
            return None
        if word == "-C":
            options.start = moved(options.start, value) if value != "" else options.start
        elif option == "--git-dir":
            options.git_dir = (True, value)
        elif option == "--work-tree":
            options.work_tree = (True, value)
        elif word == "-c":
            options.configuration.add(value)
        elif option == "--config-env":
            # The value comes from a variable the text names, which the guard does not follow; the key it settles.
            if value is None:
                options.configuration.settled = False
            else:
                options.configuration.entries.append((value.partition("=")[0].lower(), None))
        elif word == "--bare":
            options.git_dir = options.git_dir if options.git_dir[0] else (True, ".")
        elif option not in GIT_OPTIONS_WITH_VALUE and word not in GIT_OPTIONS_WITHOUT_VALUE:
            reading.notes.append(f"git's option {word} is not one the guard reads")
            return -1
        index += 2 if separate else 1
    return None


def read_git(arguments: list[Word], cwd: Path | None, variables: Variables, reading: Reading, opaque: bool) -> None:
    """One git call: its place, from the variables it sees and its own options, and its subcommand."""
    values = git_variables(variables, reading.notes)
    options = GitOptions.initial(cwd, values, opaque)
    record_git_call(arguments, options, reading)


def record_git_call(arguments: list[Word], options: GitOptions, reading: Reading) -> None:
    index = read_git_options(arguments, options, reading)
    if index is None:
        return
    if index < 0 or arguments[index] is None:
        if index >= 0:
            reading.notes.append("a git subcommand is computed at run time")
        reading.calls.append(GitCall(options.place(), None, [], options.copy()))
        return
    # A computed argument is kept as "", which no allowance matches.
    rest = [argument or "" for argument in arguments[index + 1 :]]
    reading.calls.append(GitCall(options.place(), arguments[index], rest, options.copy()))


# ---------------------------------------------------------------------------------------------------------------
# Judging a call


def allowed_form(subcommand: str, arguments: list[str]) -> bool:
    """Whether a refused subcommand is called in one of the forms that leave the hub on main and its tree alone."""
    if subcommand in {"checkout", "switch"}:
        return [argument for argument in arguments if argument not in {"-q", "--quiet"}] == [MAIN]
    if subcommand == "stash":
        return bool(arguments) and arguments[0] in {"list", "show"}
    if subcommand == "apply":
        return only_checks_a_patch(arguments)
    if subcommand == "submodule":
        return next((argument for argument in arguments if not argument.startswith("-")), None) in READ_SUBMODULE
    if subcommand == "branch":
        return not renames_a_branch(arguments)
    return False


def options_before_separator(arguments: list[str]) -> list[str]:
    """The words git reads as options: those before a `--`, after which every word is an operand."""
    return arguments[: arguments.index("--")] if "--" in arguments else arguments


def only_checks_a_patch(arguments: list[str]) -> bool:
    """Whether git apply only checks: --check among its options, and no --apply, or a prefix of it, that applies the
    patch all the same."""
    options = options_before_separator(arguments)
    applies = any(len(option) >= 4 and "--apply".startswith(option) for option in options)
    return "--check" in options and not applies


def updates_head(arguments: list[str]) -> bool:
    """Whether git fetch is given --update-head-ok, a prefix of it git accepts, or -u, alone or in a bundle, which let
    it move the branch HEAD names. -j and -o take a value, which ends a bundle."""
    for option in options_before_separator(arguments):
        if option.startswith("--"):
            name = option[2:].partition("=")[0]
            if len(name) >= len("update-h") and "update-head-ok".startswith(name):
                return True
        elif option.startswith("-") and len(option) > 1:
            for letter in option[1:]:
                if letter == "u":
                    return True
                if letter in "jo":
                    break
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
    if subcommand == FETCH:
        if updates_head(arguments):
            return "`git fetch --update-head-ok` would move the branch the hub's HEAD names."
        return None
    if subcommand not in WRITING_SUBCOMMANDS or allowed_form(subcommand, arguments):
        return None
    if subcommand in {"merge", "pull"} and "--ff-only" in arguments:
        target = [argument for argument in arguments if argument not in FAST_FORWARD_OPTIONS]
        if target in FAST_FORWARD_TARGETS[subcommand]:
            return None
        shown = " ".join(target) or "its upstream"
        return f"`git {subcommand} --ff-only` moves the hub only to origin/main, not to {shown}."
    return f"`git {subcommand}` would move the hub's HEAD or write its index or tree."


def reached_hub(place: Place) -> tuple[Path | None, bool]:
    """The guarded hub a call at place writes, and whether part of the place is not settled.

    git writes the HEAD and index of its repository (the one named, or the one it finds from where it starts) and the
    files of its work tree, so either one in a guarded hub is a write there.
    """
    unknown = place.tree is None
    hub = guarded_hub_at(place.tree) if place.tree is not None else None
    if place.repository_named:
        if place.repository is None:
            unknown = True
        else:
            hub = hub or guarded_hub_of_repository(place.repository)
    elif place.start is None:
        unknown = True
    else:
        hub = hub or guarded_hub_at(place.start)
    return hub, unknown


@dataclass
class Judge:
    """Judges a reading's calls against the guarded hubs in view of the session."""

    hubs: set[Path]
    environment: Mapping[str, str]
    notes: list[str]

    def denied(self, reading: Reading, depth: int = 0) -> str | None:
        if reading.unread:
            if self.hubs:
                return (
                    f"The guard could not read part of this command ({reading.unread[0]}), and it names git, so it is "
                    "refused while a hub is guarded. Rewrite it so each git command is written out."
                )
            self.notes.extend(reading.unread)
        for call in reading.calls:
            reason = self.judge(call, depth)
            if reason is not None:
                return reason
        return None

    def judge(self, call: GitCall, depth: int) -> str | None:
        hub, unknown = reached_hub(call.place)
        if hub is None and not (unknown and self.hubs):
            if unknown and (call.subcommand is None or refusal(call) is not None):
                self.notes.append(f"could not tell where `git {call.subcommand or '<computed>'}` runs")
            if unknown or not may_be_alias(call):
                return None
            # Placed outside every guarded hub, the call writes no hub itself, but an alias may run what does.
            return self.alias_refusal(call, alias_lookups(call.place), depth)
        if call.subcommand is None:
            return (
                "The guard could not tell which git subcommand runs, and it refuses one it cannot read where it may "
                f"write a guarded hub. {PLACE_IT if hub is None else ''}".rstrip()
            )
        reason = refusal(call)
        if reason is None and may_be_alias(call):
            hubs = [hub] if hub is not None else sorted(self.hubs)
            reason = self.alias_refusal(call, [AliasLookup(each, None, each) for each in hubs], depth)
        if reason is None or hub is not None:
            return reason
        return (
            f"The guard could not tell where `git {call.subcommand}` runs, and it refuses a git command that would "
            f"write the checkout it runs in when it cannot place it while a hub is guarded. {PLACE_IT}"
        )

    def unreadable(self, call: GitCall, why: str) -> str | None:
        """An alias that cannot be read is refused while a guarded hub is in view, unlike a command whose text cannot
        be: it hides its subcommand. With none in view it is allowed with a note."""
        message = f"The alias `git {call.subcommand}` could not be read ({why})"
        if self.hubs:
            return f"{message}, so it is refused."
        self.notes.append(message)
        return None

    def alias_refusal(self, call: GitCall, lookups: list[AliasLookup], depth: int) -> str | None:
        """Why the alias a call names would write a hub; None when it is no alias, or one that would not.

        git runs a builtin before an alias, so only a subcommand that is no builtin is looked up: first among aliases
        given on the command line, then in the configuration of each place the lookups name.
        """
        subcommand = call.subcommand or ""
        aliases = call.place.aliases
        if aliases is None:
            return self.unreadable(call, "command-line configuration the text does not settle may define it")
        if subcommand.lower() in aliases:
            expansion = aliases[subcommand.lower()]
            if expansion is None:
                return self.unreadable(call, "its value is computed at run time")
            return self.expansion_refusal(call, expansion, depth, call.place.tree)
        for lookup in lookups:
            repository = () if lookup.repository is None else ("--git-dir", str(lookup.repository))
            result = git(lookup.directory, *repository, "config", "--get", f"alias.{subcommand}")
            if result.returncode == 1:
                continue
            if result.returncode != 0:
                return self.unreadable(call, result.stderr.strip())
            reason = self.expansion_refusal(call, result.stdout.removesuffix("\n"), depth, lookup.top)
            if reason is not None:
                return reason
        return None

    def expansion_refusal(self, call: GitCall, expansion: str, depth: int, top: Path | None) -> str | None:
        """Why what an alias expands to would write a hub; a shell alias runs at top, the top of the work tree."""
        if depth >= NESTING_LIMIT:
            return self.unreadable(call, f"it nests more than {NESTING_LIMIT} deep")
        reading = Reading(self.environment, expansion)
        if expansion.startswith("!"):
            # git runs a shell alias at the top of the work tree, with the call's arguments after it.
            script = " ".join([expansion[1:], *map(shlex.quote, call.arguments)])
            reading.text = script
            read_bash(script, top, reading, depth + 1, inherited_variables(self.environment))
        else:
            try:
                words: list[Word] = list(shlex.split(expansion))
            except ValueError as exc:
                return self.unreadable(call, str(exc))
            options = call.options.copy() if call.options is not None else GitOptions.initial(None, {}, opaque=True)
            record_git_call([*words, *call.arguments], options, reading)
        reason = Judge(self.hubs, self.environment, []).denied(reading, depth + 1)
        if reason is not None:
            return f"`git {call.subcommand}` is an alias for `{expansion}`: {reason}"
        if reading.notes or reading.unread:
            return self.unreadable(call, (reading.unread or reading.notes)[0])
        return None


@dataclass(frozen=True)
class AliasLookup:
    """A place git reads an alias from: the directory it starts in, the git directory it is named, if any, and the top
    of the work tree, where a shell alias runs."""

    directory: Path
    repository: Path | None
    top: Path | None


def may_be_alias(call: GitCall) -> bool:
    """Whether a call's subcommand may be an alias: one the guard neither refuses itself nor finds among git's
    builtins, which no alias can stand for."""
    return call.subcommand is not None and call.subcommand not in WRITING_SUBCOMMANDS | {FETCH} | git_builtins()


def alias_lookups(place: Place) -> list[AliasLookup]:
    """Where git reads the aliases of a call at a settled place: the configuration of the repository it uses, found
    from where it starts or named, which a worktree shares with its hub. A place that does not exist runs nothing."""
    directory = place.start or place.tree
    if directory is None or not directory.is_dir():
        return []
    checkout = checkout_of(place.tree) if place.tree is not None and place.tree == place.start else None
    top = checkout.toplevel if checkout is not None else place.tree
    return [AliasLookup(directory, place.repository if place.repository_named else None, top)]


def denied(reading: Reading, hubs: Iterable[Path] = (), depth: int = 0) -> str | None:
    """Why a reading's calls would write a guarded hub, or None; notes on what was passed over join the reading's."""
    judge = Judge(set(hubs), reading.environment, reading.notes)
    return judge.denied(reading, depth)


# ---------------------------------------------------------------------------------------------------------------
# The Bash reader


@dataclass
class Shell:
    """A Bash shell as the guard follows it: its directory, its pushd stack, its variables, and its functions.

    variables holds every variable the shell has, by name as written, starting with the environment it inherits;
    exported, those its commands see. opaque marks a shell in which a command the guard cannot read, such as a
    sourced file, may have set any variable.
    """

    cwd: Path | None
    variables: dict[str, Word]
    exported: set[str]
    stack: list[Path | None] = field(default_factory=list)
    allexport: bool = False
    opaque: bool = False
    functions: dict[str, bash_syntax.Command] = field(default_factory=dict)

    def copy(self) -> Shell:
        return replace(
            self,
            variables=dict(self.variables),
            exported=set(self.exported),
            stack=list(self.stack),
            functions=dict(self.functions),
        )

    def lookup(self, name: str) -> Word:
        """A variable's value as the shell expands it; None when the text does not settle it."""
        if name == "PWD":
            return None if self.cwd is None else str(self.cwd)
        if self.opaque or name == "OLDPWD":
            return None
        return self.variables.get(name)

    def assign(self, name: str, value: Word, export: bool) -> None:
        """NAME=value: a variable already exported stays so, as every variable does under allexport."""
        self.variables[name] = value
        if export or self.allexport:
            self.exported.add(name)

    def unset(self, name: str) -> None:
        self.variables.pop(name, None)
        self.exported.discard(name)

    def program_variables(self, local: Mapping[str, Word]) -> dict[str, Word]:
        """The variables a program the shell starts sees: those exported, and the command's own assignments."""
        return {**{name: self.variables.get(name) for name in self.exported if name in self.variables}, **local}


_ABSENT = object()


def join(first: Shell, second: Shell) -> Shell:
    """A shell that may be either: what the two agree on stays settled, and the rest is not."""
    if first is second:
        return first
    variables: dict[str, Word] = {}
    for name in first.variables.keys() | second.variables.keys():
        left, right = first.variables.get(name, _ABSENT), second.variables.get(name, _ABSENT)
        variables[name] = left if left == right and isinstance(left, str) else None
    stack = first.stack if first.stack == second.stack else [None] * max(len(first.stack), len(second.stack))
    return Shell(
        first.cwd if same_place(first.cwd, second.cwd) else None,
        variables,
        first.exported | second.exported,
        list(stack),
        first.allexport or second.allexport,
        first.opaque or second.opaque,
        {**first.functions, **second.functions},
    )


def join_all(shells: list[Shell]) -> Shell:
    result = shells[0]
    for shell in shells[1:]:
        result = join(result, shell)
    return result


@dataclass
class Outcome:
    """The shell after a command that succeeded, and after one that failed, which && and || go on from."""

    ok: Shell
    fail: Shell

    def merged(self) -> Shell:
        return join(self.ok, self.fail)


def certain(shell: Shell) -> Outcome:
    return Outcome(shell, shell)


GLOB = re.compile(r"[*?]|\[.*\]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
SPLIT = re.compile(r"[\s*?\[]")


def word_value(word: bash_syntax.Word, shell: Shell, assignment: bool = False) -> Word:
    """The text Bash makes of a word; None when only running settles it: an expansion of what the text does not
    set, a substitution, or an unquoted glob, brace, or value that splits into words. An assignment's value is neither
    split nor globbed."""
    pieces: list[str] = []
    for part in word.parts:
        if part.kind == "text":
            if not part.quoted and not assignment and GLOB.search(part.text):
                return None
            pieces.append(part.text)
        elif part.kind == "parameter":
            value = shell.lookup(part.text)
            if value is None or (not part.quoted and not assignment and SPLIT.search(value)):
                return None
            pieces.append(value)
        elif part.kind == "home":
            pieces.append(str(Path.home()))
        else:
            return None
    return "".join(pieces)


def assigned_value(assignment: bash_syntax.Assignment, shell: Shell) -> Word:
    """The value a simple command's assignment gives its variable: unsettled when it appends, or when it names an
    element and so makes the variable an array."""
    if assignment.appends or assignment.element:
        return None
    return word_value(bash_syntax.Word("", assignment.value), shell, assignment=True)


class BashReader:
    """Follows a Bash command tree as Bash runs it, recording each git call with where it acts.

    A part that runs only when another succeeds or fails, or only in some passes of a loop, leaves the shell as either
    outcome might; what both agree on stays settled. A subshell, a pipeline's commands, a background job, and every
    command or process substitution run in a copy whose changes die with it.
    """

    def __init__(self, reading: Reading, depth: int, builtin: str = "") -> None:
        self.reading = reading
        self.depth = depth
        # The name a builtin was called by, which export, declare, command, and the rest read.
        self.current_builtin = builtin

    def with_builtin(self, name: str) -> BashReader:
        return BashReader(self.reading, self.depth, name)

    def run_text(self, text: str, shell: Shell) -> Outcome:
        try:
            tree = bash_syntax.parse(text)
        except bash_syntax.BashSyntaxError as exc:
            self.reading.cannot_read(f"Bash would not read it as written, or the guard cannot ({exc})")
            lost = shell.copy()
            lost.cwd, lost.opaque = None, True
            return certain(lost)
        return self.run_list(tree, shell)

    def run_list(self, node: bash_syntax.CommandList, shell: Shell) -> Outcome:
        outcome = certain(shell)
        for and_or, background in node.items:
            state = outcome.merged()
            if background:
                self.run_and_or(and_or, state.copy())
                outcome = certain(state)
            else:
                outcome = self.run_and_or(and_or, state)
        return outcome

    def run_and_or(self, node: bash_syntax.AndOr, shell: Shell) -> Outcome:
        outcome = self.run_pipeline(node.first, shell)
        for operator, pipeline in node.rest:
            if operator == "&&":
                after = self.run_pipeline(pipeline, outcome.ok.copy())
                outcome = Outcome(after.ok, join(outcome.fail, after.fail))
            else:
                after = self.run_pipeline(pipeline, outcome.fail.copy())
                outcome = Outcome(join(outcome.ok, after.ok), after.fail)
        return outcome

    def run_pipeline(self, node: bash_syntax.Pipeline, shell: Shell) -> Outcome:
        if len(node.commands) == 1:
            outcome = self.run_command(node.commands[0], shell)
        else:
            for command in node.commands:
                self.run_command(command, shell.copy())
            outcome = certain(shell)
        return Outcome(outcome.fail, outcome.ok) if node.negated else outcome

    def run_command(self, node: bash_syntax.Command, shell: Shell) -> Outcome:
        if isinstance(node, bash_syntax.Simple):
            return self.run_simple(node, shell)
        if isinstance(node, bash_syntax.FunctionDefinition):
            shell.functions[node.name] = node.body
            return certain(shell)
        self.read_substitutions([], node.redirects, shell)
        if isinstance(node, bash_syntax.Subshell):
            self.run_list(node.body, shell.copy())
            return certain(shell)
        if isinstance(node, bash_syntax.Group):
            return self.run_list(node.body, shell)
        if isinstance(node, bash_syntax.If):
            return self.run_if(node, shell)
        if isinstance(node, bash_syntax.Case):
            self.read_substitutions([node.word], [], shell)
            branches = [self.run_list(branch, shell.copy()).merged() for branch in node.branches]
            return certain(join_all([shell, *branches]))
        return self.run_loop(node, shell)

    def run_if(self, node: bash_syntax.If, shell: Shell) -> Outcome:
        results: list[Shell] = []
        state = shell
        for condition, body in node.clauses:
            tested = self.run_list(condition, state)
            results.append(self.run_list(body, tested.ok.copy()).merged())
            state = tested.fail.copy()
        results.append(self.run_list(node.otherwise, state).merged() if node.otherwise is not None else state)
        return certain(join_all(results))

    def run_loop(self, node: bash_syntax.Loop, shell: Shell) -> Outcome:
        """A loop's body may run any number of times: two passes, the second from either the entry or the first pass's
        end, reach every state the guard tells apart."""
        if node.words is not None:
            self.read_substitutions(node.words, [], shell)
        seen = [shell]
        state = shell.copy()
        for _ in range(2):
            if node.condition is not None:
                tested = self.run_list(node.condition, state)
                seen.append((tested.ok if node.kind == "until" else tested.fail).copy())
                state = (tested.fail if node.kind == "until" else tested.ok).copy()
            if node.name is not None:
                state.assign(node.name, None, export=False)
            after = self.run_list(node.body, state).merged()
            seen.append(after)
            state = join(shell, after).copy()
        return certain(join_all(seen))

    def read_substitutions(
        self, words: list[bash_syntax.Word], redirects: list[bash_syntax.Redirect], shell: Shell
    ) -> None:
        """Each command and process substitution runs, in a subshell, before the command it is part of."""
        texts = [text for word in words for text in word.substitutions]
        for redirect in redirects:
            if redirect.target is not None:
                texts.extend(redirect.target.substitutions)
            if redirect.here is not None:
                texts.extend(redirect.here.substitutions)
        for text in texts:
            self.run_text(text, shell.copy())

    def run_simple(self, node: bash_syntax.Simple, shell: Shell) -> Outcome:
        self.read_substitutions(node.words, node.redirects, shell)
        words = node.words
        if words and words[0].literal == "[[":
            return certain(shell)
        index = 0
        while index < len(words) and words[index].assignment is not None:
            index += 1
        assignments = [word.assignment for word in words[:index] if word.assignment is not None]
        if index == len(words):
            assigned = [(assignment.name, assigned_value(assignment, shell)) for assignment in assignments]
            for name, value in assigned:
                shell.assign(name, value, export=name in shell.exported)
            return certain(shell)
        # Before a command's name Bash refuses an element's assignment, NAME[SUBSCRIPT]=, sets nothing for it, and
        # runs the command all the same.
        local = {
            assignment.name: assigned_value(assignment, shell) for assignment in assignments if not assignment.element
        }
        values = [word_value(word, shell) for word in words[index:]]
        return self.run_words(values, node.redirects, shell, local)

    def run_words(
        self, values: list[Word], redirects: list[bash_syntax.Redirect], shell: Shell, local: dict[str, Word]
    ) -> Outcome:
        name, arguments = values[0], values[1:]
        if name is None:
            return certain(shell)
        if name in shell.functions:
            return self.call_function(name, shell)
        builtin = BASH_BUILTINS.get(name)
        if builtin is not None:
            return builtin(self.with_builtin(name), arguments, redirects, shell, local)
        self.run_program(command_name(name), arguments, redirects, shell, shell.program_variables(local))
        return certain(shell)

    def call_function(self, name: str, shell: Shell) -> Outcome:
        if self.depth >= NESTING_LIMIT:
            self.reading.cannot_read(f"the function {name} nests more than {NESTING_LIMIT} deep")
            lost = shell.copy()
            lost.cwd, lost.opaque = None, True
            return certain(lost)
        return BashReader(self.reading, self.depth + 1).run_command(shell.functions[name], shell)

    def run_program(
        self,
        program: str | None,
        arguments: list[Word],
        redirects: list[bash_syntax.Redirect],
        shell: Shell,
        variables: dict[str, Word],
    ) -> None:
        """A program a Bash command starts, in the shell's directory and with these variables in its environment."""
        if program in GIT_NAMES:
            read_git(arguments, shell.cwd, variables, self.reading, shell.opaque)
        elif program in BASH_NAMES:
            script = bash_script(arguments, redirects, shell)
            follow(program, script, shell.cwd, variables, self.reading, self.depth, read_bash)
        elif program in POWERSHELL_NAMES:
            script = [None] if powershell_reads_input(arguments) else powershell_script(arguments)
            follow(program, script, shell.cwd, variables, self.reading, self.depth, read_powershell)
        elif program in ENV_NAMES or program in PREFIX_PROGRAMS:
            command = started_command(program, arguments, shell.cwd, variables, self.reading)
            if command is not None:
                name, rest, cwd, given = command
                started = shell.copy()
                started.cwd = cwd
                self.run_program(name, rest, redirects, started, given)


def started_command(
    program: str, arguments: list[Word], cwd: Path | None, variables: Variables, reading: Reading
) -> tuple[str | None, list[Word], Path | None, dict[str, Word]] | None:
    """The command env or a prefix program such as nice starts: its name, its arguments, its directory, and its
    environment; None when it starts none, and when its options cannot be read, which is recorded as unread.

    env starts its program with the environment it is given: emptied by -i, less each -u, plus each NAME=value, and in
    the directory -C names."""
    if program not in ENV_NAMES:
        command = prefixed_command(program, arguments)
        if command is None:
            reading.cannot_read(f"could not read the options passed to {program}")
            return None
        return (command_name(command[0]), command[1:], cwd, dict(variables)) if command else None
    read = read_env_options(arguments, cwd, variables)
    if read is None:
        reading.cannot_read(f"could not read the option {first_unread_env_option(arguments)} passed to env")
        return None
    index, cwd, given = read
    # env takes every word holding `=` before its command as NAME=value, whatever the name: a[1]=x and `x y=1` too.
    while index < len(arguments) and "=" in (arguments[index] or ""):
        name, _, assigned = (arguments[index] or "").partition("=")
        given[name] = assigned
        index += 1
    if index < len(arguments):
        return command_name(arguments[index]), arguments[index + 1 :], cwd, given
    return None


def read_env_options(
    arguments: list[Word], cwd: Path | None, variables: Mapping[str, Word]
) -> tuple[int, Path | None, dict[str, Word]] | None:
    """env's options: where its command starts, its directory, and the environment it gives; None when one cannot
    be read.

    A short option's value is read attached, as in `-uNAME` and `-CDIR`, or as the next word. env changes directory
    once, to the last -C it is given, read from where env starts."""
    given = dict(variables)
    directory: tuple[Word] | None = None
    index = 0
    while index < len(arguments):
        word = arguments[index]
        value = arguments[index + 1] if index + 1 < len(arguments) else None
        if word is None or not word.startswith("-") or word == "--":
            index += word == "--"
            break
        option, _, inline_value = word.partition("=")
        # The value of -u or -C: the next word after the option alone, else after a long option's =, else attached.
        separate = word in ENV_UNSET | ENV_CHDIR
        named = value if separate else inline_value if word.startswith("--") else word[2:]
        if word in ENV_CLEAR:
            given.clear()
        elif option == "--unset" or word.startswith("-u"):
            given.pop(named or "", None)
        elif option == "--chdir" or word.startswith("-C"):
            directory = (named,)
        elif word not in ENV_FLAGS:
            return None
        index += 2 if separate else 1
    return index, cwd if directory is None else moved(cwd, directory[0]), given


def first_unread_env_option(arguments: list[Word]) -> str:
    """The first option read_env_options does not read, past the value of each -u and -C given as the next word."""
    known = ENV_FLAGS | ENV_UNSET | ENV_CHDIR | ENV_CLEAR
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith("-"):
            break
        if word not in known and not word.startswith(("-u", "-C", "--unset=", "--chdir=")):
            return word
        index += 2 if word in ENV_UNSET | ENV_CHDIR else 1
    return "?"


def prefixed_command(program: str, arguments: list[Word]) -> list[Word] | None:
    """The command a prefix program such as nohup, nice, or timeout starts; None when its options cannot be read.

    A short option that takes a value takes the rest of its word when anything follows it there, as in `nice -n5`
    and `stdbuf -oL`, the way getopt reads it; any other bundle of short options is not read."""
    flags, valued, operands = PREFIX_PROGRAMS[program]
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None:
            return None
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or word == "-":
            break
        option = word.partition("=")[0]
        if word in flags or (program == "nice" and word[1:].isdigit()):
            index += 1
        elif option in valued:
            index += 1 if "=" in word else 2
        elif not word.startswith("--") and word[:2] in valued:
            index += 1
        else:
            return None
    index += operands
    return arguments[index:] if index <= len(arguments) else []


def bash_cd(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    """Where Bash's cd leaves the shell: home when bare, unknown for `cd -` or a CDPATH search; with an unknown
    option or two operands it fails and stays."""
    index = 0
    while index < len(arguments) and (word := arguments[index]) is not None and word.startswith("-") and word != "-":
        index += 1
        if word == "--":
            break
        if not set(word[1:]) <= set("LPe@"):
            return certain(shell)
    operands = arguments[index:]
    if len(operands) > 1:
        return certain(shell)
    target = operands[0] if operands else str(Path.home())
    if target is None or target == "-":
        return change_directory(shell, None)
    searched = shell.variables.get("CDPATH", "") != "" and not target.startswith((".", "/", "~"))
    if searched and not is_absolute(target):
        return change_directory(shell, None)
    return change_directory(shell, moved(shell.cwd, target))


def change_directory(shell: Shell, target: Path | None, push: bool = False) -> Outcome:
    """A directory change that fails, and leaves the shell where it was, when its target is not a directory."""
    moved_shell = shell.copy()
    if push:
        moved_shell.stack.append(shell.cwd)
    moved_shell.cwd = target
    if target is not None and target.is_dir():
        return certain(moved_shell)
    return Outcome(moved_shell, shell)


def bash_pushd(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    operands = [word for word in arguments if word != "--"]
    if len(operands) == 1 and operands[0] is not None and not operands[0].startswith(("-", "+")):
        return change_directory(shell, moved(shell.cwd, operands[0]), push=True)
    pushed = shell.copy()
    pushed.stack.append(shell.cwd)
    pushed.cwd = None
    return certain(pushed)


def bash_popd(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    popped = shell.copy()
    popped.cwd = popped.stack.pop() if popped.stack and not arguments else None
    return certain(popped)


def bash_declare(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    """export, declare, typeset, local, and readonly: the variables they set, export, or stop exporting."""
    builtin = reader.current_builtin
    exporting, unexporting = builtin == "export", False
    for word in arguments:
        if word is None:
            shell.opaque = True
            continue
        if word.startswith(("-", "+")) and not ASSIGNMENT.match(word):
            flags = word[1:]
            if "f" in flags:
                return certain(shell)  # Functions, not variables.
            if builtin == "export" and "n" in flags:
                unexporting = True
            elif "x" in flags:
                exporting, unexporting = word.startswith("-"), word.startswith("+")
            continue
        name, has_value, value = word.partition("=")
        if has_value:
            shell.variables[name] = value
        if unexporting:
            shell.exported.discard(name)
        elif exporting or (shell.allexport and has_value):
            shell.exported.add(name)
    return certain(shell)


def bash_unset(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    functions = "-f" in arguments
    for word in arguments:
        if word is None:
            shell.opaque = True
        elif not word.startswith("-"):
            if functions:
                shell.functions.pop(word, None)
            else:
                shell.unset(word)
    return certain(shell)


def bash_set(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    """set -a, set -o allexport, and their + forms, which export every variable assigned while they hold."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in {"-o", "+o"}:
            if arguments[index + 1 : index + 2] == ["allexport"]:
                shell.allexport = word == "-o"
            index += 2
            continue
        if word is None or word == "--" or len(word) < 2 or word[0] not in "-+" or not word[1:].isalpha():
            break
        if "a" in word[1:]:
            shell.allexport = word[0] == "-"
        index += 1
    return certain(shell)


def bash_eval(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    """eval runs its words, joined, as a command of this shell."""
    if None in arguments or reader.depth >= NESTING_LIMIT:
        reader.reading.cannot_read("could not read the command passed to eval")
        lost = shell.copy()
        lost.cwd, lost.opaque = None, True
        return certain(lost)
    text = " ".join(word for word in arguments if word is not None)
    return BashReader(reader.reading, reader.depth + 1).run_text(text, shell)


def bash_source(reader: BashReader, arguments: list[Word], _: object, shell: Shell, __: object) -> Outcome:
    """A sourced file runs in this shell, where the guard does not read it: it may change the directory and any
    variable, so both are unknown after it."""
    reader.reading.notes.append(
        "a sourced file may change the directory and the environment, which the guard does not read"
    )
    lost = shell.copy()
    lost.cwd, lost.opaque = None, True
    return certain(lost)


def bash_exec(
    reader: BashReader,
    arguments: list[Word],
    redirects: list[bash_syntax.Redirect],
    shell: Shell,
    local: dict[str, Word],
) -> Outcome:
    """exec, command, and builtin run the command after their own options."""
    index = 0
    while index < len(arguments) and (word := arguments[index]) is not None and word.startswith("-"):
        if reader.current_builtin == "command" and set(word[1:]) & {"v", "V"}:
            return certain(shell)
        index += 2 if word == "-a" else 1
        if word == "--":
            break
    rest = arguments[index:]
    if not rest:
        return certain(shell)
    if reader.current_builtin == "builtin" and rest[0] not in BASH_BUILTINS:
        return certain(shell)
    if rest[0] is not None and rest[0] in BASH_BUILTINS:
        return BASH_BUILTINS[rest[0]](reader.with_builtin(rest[0]), rest[1:], redirects, shell, local)
    reader.run_program(command_name(rest[0]), rest[1:], redirects, shell, shell.program_variables(local))
    return certain(shell)


BuiltinReader = Callable[[BashReader, list[Word], list[bash_syntax.Redirect], Shell, dict[str, Word]], Outcome]
BASH_BUILTINS: dict[str, BuiltinReader] = {
    "cd": bash_cd,
    "pushd": bash_pushd,
    "popd": bash_popd,
    **dict.fromkeys(DECLARE_NAMES, bash_declare),
    "unset": bash_unset,
    "set": bash_set,
    "eval": bash_eval,
    "source": bash_source,
    ".": bash_source,
    **dict.fromkeys(("exec", "command", "builtin"), bash_exec),
}


def bash_script(arguments: list[Word], redirects: list[bash_syntax.Redirect], shell: Shell) -> list[Word] | None:
    """The one word bash runs from -c, alone or in a bundle such as -lc, or, with no script file, the here-string or
    here-document it reads; None for none of them."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith(("-", "+")) or word == "--":
            index += word == "--"
            break
        if not word.startswith("--") and "c" in word[1:]:
            return arguments[index + 1 : index + 2]
        index += 2 if word in BASH_OPTIONS_WITH_VALUE else 1
    if index < len(arguments):
        return None  # a script file, which the guard does not read
    script: list[Word] | None = None
    for redirect in redirects:
        if redirect.operator == "<<<" and redirect.target is not None:
            script = [word_value(redirect.target, shell, assignment=True)]
        elif redirect.here is not None:
            here = redirect.here
            settled = here.quoted or not re.search(r"[$`\\]", here.body)
            script = [here.body if settled else None]
        elif redirect.operator in {"<", "<>"}:
            script = None
    return script


def powershell_script(arguments: list[Word]) -> list[Word] | None:
    """The script pwsh runs inline: every word after -Command, or an abbreviation of it, or the one word after
    -CommandWithArgs or -cwa, whose other words are its arguments; None when it runs no inline script."""
    for index, word in enumerate(arguments):
        if word is not None and POWERSHELL_COMMAND_WITH_ARGUMENTS_SWITCH.fullmatch(word):
            return arguments[index + 1 : index + 2]
        if word is not None and POWERSHELL_COMMAND_SWITCH.fullmatch(word):
            return arguments[index + 1 :]
    return None


def powershell_reads_input(arguments: list[Word]) -> bool:
    """Whether pwsh runs a script it reads from its standard input, which the text does not hold: -Command - or
    -File -, or none of an inline command, a script file, or an encoded command. A word the text does not settle
    before that is decided may be any of them, so it counts too.

    The parameters are spelled as in powershell_takes_value."""
    encoded = (("encodedcommand", "e"), ("ec", "ec"))
    file = (("file", "f"),)
    index = 0
    while index < len(arguments):
        word = arguments[index]
        following = arguments[index + 1] if index + 1 < len(arguments) else None
        if word is None:
            return True
        if POWERSHELL_COMMAND_WITH_ARGUMENTS_SWITCH.fullmatch(word) or powershell_parameter(word, encoded):
            return False
        if POWERSHELL_COMMAND_SWITCH.fullmatch(word) or powershell_parameter(word, file):
            return following == "-"
        if not word.startswith("-"):
            return False  # a script file, named without -File
        index += 2 if powershell_takes_value(word) else 1
    return True


def powershell_takes_value(word: str) -> bool:
    """Whether a pwsh parameter takes the next word as its value.

    Each is given by its name and the shortest prefix of it pwsh accepts, and each alias by itself. The table sits in
    the function, not the module, so that it runs in the tests that call the function rather than at import."""
    takes_value = (
        ("configurationfile", "configurationfile"),
        ("configurationname", "config"),
        ("custompipename", "custompipename"),
        ("executionpolicy", "ex"),
        ("ep", "ep"),
        ("inputformat", "inp"),
        ("if", "if"),
        ("outputformat", "o"),
        ("of", "of"),
        ("settingsfile", "settings"),
        ("windowstyle", "w"),
        ("workingdirectory", "wo"),
        ("wd", "wd"),
    )
    return powershell_parameter(word, takes_value)


def powershell_parameter(word: str, forms: Iterable[tuple[str, str]]) -> bool:
    """Whether a word names one of these pwsh parameters, in any case: a prefix of a name at least as long as its
    shortest form."""
    name = word[1:].lower()
    return word.startswith("-") and any(full.startswith(name) and name.startswith(short) for full, short in forms)


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
    if not script or None in script or depth >= NESTING_LIMIT:
        reading.cannot_read(f"could not read the command passed to {name}")
        return
    read(" ".join(word for word in script if word is not None), cwd, reading, depth + 1, variables)


def read_bash(command: str, cwd: Path | None, reading: Reading, depth: int, variables: Variables) -> None:
    """Reads a Bash command, started with these variables exported, as Bash runs it."""
    allexport = "allexport" in reading.environment.get("SHELLOPTS", "").split(":")
    # A shell inherits the environment the hook was given; the tracked variables are those its starter passes on.
    untracked = {name: value for name, value in reading.environment.items() if not is_tracked(name)}
    inherited: dict[str, Word] = {**untracked, **variables}
    shell = Shell(cwd, dict(inherited), set(inherited), allexport=allexport)
    BashReader(reading, depth).run_text(command, shell)


# ---------------------------------------------------------------------------------------------------------------
# The PowerShell reader


@functools.cache
def powershell_reader() -> str:
    return POWERSHELL_READER.read_text(encoding="utf-8")


def powershell_events(command: str, reading: Reading, depth: int) -> list[dict[str, Any]] | None:
    """The reader's events for one command, or None when there are none, with why.

    pwsh starts with -Command rather than -File, so no execution policy applies, and not with -EncodedCommand,
    which endpoint protection treats as a sign of malware. The command, and how much nesting is left to follow,
    travel base64-encoded on stdin, past any console code page.
    """
    pwsh = platform_support.find_pwsh(reading.environment.get("PATH", ""))
    if pwsh is None:
        reading.cannot_read("could not find pwsh to read a PowerShell command")
        return None
    payload = json.dumps({"command": command, "limit": NESTING_LIMIT - depth})
    try:
        # run_bounded takes git's repository-selecting variables out of the hook's environment, and the tree ends
        # every process pwsh starts once it has finished or been killed.
        result = run_bounded(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershell_reader()],
            POWERSHELL_TIMEOUT_SECONDS,
            input_bytes=base64.b64encode(payload.encode("utf-8")),
            env=reading.environment,
            output_limit=POWERSHELL_OUTPUT_LIMIT,
            tree=process_tree(hidden_window=True),
        )
    except subprocess.TimeoutExpired:
        reading.cannot_read(f"pwsh did not read the command within {POWERSHELL_TIMEOUT_SECONDS} seconds")
        return None
    except OSError as exc:
        reading.cannot_read(f"could not start pwsh ({exc})")
        return None
    try:
        output = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        errors = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reading.cannot_read(f"pwsh could not read the command ({(errors or [''])[0]})")
        return None
    if "error" in output:
        # PowerShell parses a whole script before it runs any of it, so a command that does not parse runs
        # nothing; a Bash command's lines before a syntax error do run.
        reading.notes.append(f"PowerShell would not parse the command, so none of it runs ({output['error']})")
        return None
    events: list[dict[str, Any]] = output["events"]
    return events


def mentions_alias(command: str, directories: Iterable[Path]) -> bool:
    """Whether the text names an alias git defines in any of these checkouts; true when one cannot be listed.

    git lists them in the environment the hook was given, which is the one the command runs in."""
    for directory in directories:
        result = git(directory, "config", "--name-only", "--get-regexp", r"^alias\.")
        if result.returncode == 1:
            continue
        if result.returncode != 0:
            return True
        names = (line.partition(".")[2] for line in result.stdout.splitlines())
        for name in filter(None, names):
            if mentions(re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE), command):
                return True
    return False


def may_refuse(command: str, cwd: Path | None, reading: Reading) -> bool:
    """Whether the text could hold a git call the guard refuses: what pwsh is started to read.

    It names git and, as a word, a refused subcommand or fetch, an alias git defines where the command starts or in
    a hub in view, or command-line alias configuration; or git is followed in its statement by something that computes
    a word; or the command starts outside every repository, where the aliases a hub defines cannot be listed.
    """
    if not mentions(MENTIONS_GIT, command):
        return False
    if mentions(MENTIONS_WRITING_SUBCOMMAND, command) or COMPUTED_AFTER_GIT.search(command) or "alias." in command:
        return True
    if cwd is None:
        return True
    try:
        if checkout_of(cwd) is None:
            return True
    except GitError:
        return True  # A checkout git cannot read lists no aliases either.
    return mentions_alias(command, [cwd, *sorted(reading.context_hubs)])


@dataclass
class Region:
    """Events PowerShell runs only on some paths: a branch runs once or not at all, a loop or a script block any number
    of times, a function's body only where a command calls it, and a child pwsh in a process of its own."""

    kind: str
    name: str
    children: list[Region | dict[str, Any]] = field(default_factory=list)


def event_tree(events: list[dict[str, Any]]) -> list[Region | dict[str, Any]]:
    root = Region("root", "")
    stack = [root]
    for event in events:
        kind = event.get("kind")
        if kind == "open":
            region = Region(str(event.get("region")), str(event.get("name") or ""))
            stack[-1].children.append(region)
            stack.append(region)
        elif kind == "close":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].children.append(event)
    return root.children


@dataclass
class Session:
    """A PowerShell session's location, its Push-Location stack, the tracked variables of its environment, and its
    functions. opaque marks a session in which a command the guard cannot read may have set any variable."""

    cwd: Path | None
    variables: dict[str, Word]
    stack: list[Path | None] = field(default_factory=list)
    opaque: bool = False
    functions: dict[str, list[Region | dict[str, Any]]] = field(default_factory=dict)

    def copy(self) -> Session:
        return replace(self, variables=dict(self.variables), stack=list(self.stack), functions=dict(self.functions))

    def lookup(self, path: str) -> Word:
        """The value of the variable a path names on the environment provider, as the session holds it; None for one
        it does not hold or does not settle, or a path elsewhere."""
        match = ENVIRONMENT_ITEM.match(path)
        if self.opaque or match is None:
            return None
        return self.variables.get(folded(match.group(1)))

    def word(self, word: Mapping[str, Any]) -> Word:
        """A word the reader reports: its text, or, for one that holds environment variables, its text with the value
        each has in this session, which the command's own assignments set; None when the text does not settle it."""
        if "variable" in word:
            return self.lookup(str(word["variable"]))
        if "expansions" in word:
            text = str(word["text"])
            for expansion in word["expansions"]:
                value = self.lookup(str(expansion["variable"]))
                if value is None:
                    return None
                text = text.replace(str(expansion["token"]), value)
            return text
        return None if word["dynamic"] else str(word["text"])


def join_sessions(first: Session, second: Session) -> Session:
    variables: dict[str, Word] = {}
    for name in first.variables.keys() | second.variables.keys():
        left, right = first.variables.get(name, _ABSENT), second.variables.get(name, _ABSENT)
        variables[name] = left if left == right and isinstance(left, str) else None
    stack = first.stack if first.stack == second.stack else [None] * max(len(first.stack), len(second.stack))
    return Session(
        first.cwd if same_place(first.cwd, second.cwd) else None,
        variables,
        list(stack),
        first.opaque or second.opaque,
        {**first.functions, **second.functions},
    )


def environment_names(paths: list[Word], literal: bool, known: Iterable[str] = ()) -> list[str] | None:
    """The variables these provider paths name, by the name git reads; None when a path is not settled. A path that is
    not on the environment provider names none; a wildcard in a -Path names every one it matches among those known and
    the tracked ones."""
    found: list[str] = []
    for path in paths:
        if path is None:
            return None
        match = ENVIRONMENT_ITEM.match(path)
        if match is None:
            continue
        name = match.group(1)
        if not literal and any(character in name for character in "*?["):
            candidates = {*known, *LOCATION_VARIABLES, *CONFIGURATION_VARIABLES}
            found.extend(sorted(variable for variable in candidates if fnmatch.fnmatchcase(variable, folded(name))))
        else:
            found.append(folded(name))
    return found


class PowerShellReader:
    def __init__(self, reading: Reading, depth: int) -> None:
        self.reading = reading
        self.depth = depth

    def run(self, nodes: list[Region | dict[str, Any]], session: Session) -> Session:
        for node in nodes:
            session = self.region(node, session) if isinstance(node, Region) else self.event(node, session)
        return session

    def region(self, region: Region, session: Session) -> Session:
        if region.kind == "function":
            session.functions[region.name.lower()] = region.children
            return session
        if region.kind == "child":
            self.run(region.children, session.copy())
            return session
        first = self.run(region.children, session.copy())
        if region.kind == "branch":
            return join_sessions(session, first)
        second = self.run(region.children, join_sessions(session, first))
        return join_sessions(join_sessions(session, first), second)

    def event(self, event: dict[str, Any], session: Session) -> Session:
        kind = event.get("kind")
        if kind == "command":
            return self.command(event, session)
        if kind == "assigned":
            self.assigned(event, session)
        elif kind == "setVariable":
            variable = event.get("name")
            if variable is None:
                session.opaque = True
            elif event.get("removed"):
                session.variables.pop(folded(str(variable)), None)
            else:
                session.variables[folded(str(variable))] = None
        elif kind == "opaque":
            session.opaque = True
        elif kind == "unread":
            self.reading.cannot_read(str(event.get("text")))
        elif kind == "note":
            self.reading.notes.append(str(event.get("text")))
        return session

    @staticmethod
    def assigned(event: dict[str, Any], session: Session) -> None:
        """An assignment to a variable on the environment provider: a plain one sets the value the reader reports, an
        empty one or $null removes the variable, as PowerShell does, and any other leaves its value unsettled."""
        value = session.word(event["value"]) if "value" in event else None
        for name in environment_names([str(event.get("path", ""))], literal=True) or []:
            if event.get("removed") or value == "":
                session.variables.pop(name, None)
            else:
                session.variables[name] = value

    def command(self, event: dict[str, Any], session: Session) -> Session:
        words: list[Word] = [session.word(word) for word in event["words"]]
        parameters = {
            name.lower(): [session.word(word) for word in values]
            for name, values in (event.get("parameters") or {}).items()
        }
        # The words the reader reports for each parameter, from which a path the text does not settle is judged.
        reported = {name.lower(): values for name, values in (event.get("parameters") or {}).items()}
        name, arguments = command_name(words[0]), words[1:]
        if event.get("failed") or event.get("whatIf"):
            return session  # A command PowerShell cannot bind, or one told -WhatIf, runs nothing.
        if name is not None and name in session.functions:
            return self.call_function(name, session)
        if name in LOCATION_NAMES | PUSH_LOCATION_NAMES | POP_LOCATION_NAMES:
            return self.location(name, arguments, parameters, session)
        if name in GIT_NAMES | BASH_NAMES | ENV_NAMES or name in PREFIX_PROGRAMS:
            self.run_program(name, arguments, session.cwd, session.variables, session.opaque)
        elif name in POWERSHELL_NAMES:
            # The reader follows a pwsh the session starts with an inline script; one it reads from its standard
            # input, the text does not hold.
            if powershell_reads_input(arguments):
                self.reading.cannot_read(f"could not read the command passed to {name}")
        else:
            self.environment_change(name, parameters, session, reported)
        return session

    def run_program(
        self, name: str | None, arguments: list[Word], cwd: Path | None, variables: Variables, opaque: bool
    ) -> None:
        """A program a PowerShell command starts, in this directory and with these variables in its environment. A pwsh
        the session starts itself is read by the PowerShell reader; one that env or a prefix program starts, here."""
        if name in GIT_NAMES:
            read_git(arguments, cwd, variables, self.reading, opaque)
        elif name in BASH_NAMES:
            script = bash_script(arguments, [], Shell(None, {}, set()))
            follow(name, script, cwd, variables, self.reading, self.depth, read_bash)
        elif name in POWERSHELL_NAMES:
            script = [None] if powershell_reads_input(arguments) else powershell_script(arguments)
            follow(name, script, cwd, variables, self.reading, self.depth, read_powershell)
        elif name in ENV_NAMES or name in PREFIX_PROGRAMS:
            command = started_command(name, arguments, cwd, variables, self.reading)
            if command is not None:
                self.run_program(*command, opaque)

    def call_function(self, name: str, session: Session) -> Session:
        if self.depth >= NESTING_LIMIT:
            self.reading.cannot_read(f"the function {name} nests more than {NESTING_LIMIT} deep")
            session.cwd, session.opaque = None, True
            return session
        return PowerShellReader(self.reading, self.depth + 1).run(session.functions[name], session)

    def location(
        self, name: str, arguments: list[Word], parameters: Mapping[str, list[Word]], session: Session
    ) -> Session:
        """Set-Location and Push-Location go where the path PowerShell binds names; bare, Set-Location goes home and
        Push-Location stays. A location on a provider other than the file system, or a path that is not a directory,
        which makes the command fail where it stands, leaves where git starts unknown or as it was."""
        if name in POP_LOCATION_NAMES:
            session.cwd = session.stack.pop() if session.stack else None
            return session
        paths = parameters.get("path", []) + parameters.get("literalpath", [])
        if not paths and arguments and all(word in {"-", "+"} for word in arguments):
            paths = [None]  # `cd -` and `cd +` walk a history the text does not show
        pushing = name in PUSH_LOCATION_NAMES
        if not paths and pushing:
            session.stack.append(session.cwd)
            return session
        target = location_after(session.cwd, bool(paths), paths[0] if len(paths) == 1 else None)
        if paths and paths[0] is not None and PROVIDER_LOCATION.match(paths[0]):
            target, session.opaque = None, True
        moved_session = session.copy()
        if pushing:
            moved_session.stack.append(session.cwd)
        moved_session.cwd = target
        if target is not None and target.is_dir():
            return moved_session
        return join_sessions(moved_session, session)

    def environment_change(
        self,
        name: str | None,
        parameters: Mapping[str, list[Word]],
        session: Session,
        reported: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        """Sets, removes, renames, or makes unknown the tracked variables a cmdlet reaches on the environment
        provider, read from the parameters PowerShell binds.

        A path or name the text does not settle, in a parameter that names the variable such a cmdlet writes or
        removes, may name any variable, so the session's variables are unknown after it, as they are after Bash's
        `export "$v"=...`, unless may_name_tracked passes it over. reported holds the words the reader reports for each
        parameter."""
        literal = parameters.get("literalpath", [])
        paths = parameters.get("path", []) + literal
        reached = set(ENVIRONMENT_WRITERS.get(name or "", ()))
        if name in ENVIRONMENT_REMOVERS or name in RENAME_ITEM_NAMES:
            reached |= {"path", "literalpath"}
        # -Name and -NewName name a variable only where the path may be on the environment provider.
        if (name in NEW_ITEM_NAMES or name in RENAME_ITEM_NAMES) and any(
            path is None or ENVIRONMENT_PATH.match(path) for path in paths
        ):
            reached |= {"name", "newname"}
        words = [word for parameter in sorted(reached) for word in (reported or {}).get(parameter, [])]
        if any(session.word(word) is None and may_name_tracked(word) for word in words):
            session.opaque = True
        if name in NEW_DRIVE_NAMES:
            provider = (parameters.get("psprovider") or [None])[0]
            if provider is None or "environment" in provider.lower():
                session.opaque = True
            return
        known = session.variables.keys()
        settled = environment_names(parameters.get("path", []), literal=False, known=known) or []
        settled += environment_names(literal, literal=True) or []
        for parameter in ENVIRONMENT_WRITERS.get(name or "", ()):
            written = parameters.get(parameter, [])
            for variable in environment_names(written, literal=parameter == "literalpath", known=known) or []:
                session.variables[variable] = None
        if name in ENVIRONMENT_REMOVERS:
            for variable in settled:
                session.variables.pop(variable, None)
        elif name in NEW_ITEM_NAMES and any(ENVIRONMENT_ROOT.match(path or "") for path in paths):
            self.set_named(parameters.get("name", []), session)
        elif name in RENAME_ITEM_NAMES and any(ENVIRONMENT_PATH.match(path or "") for path in paths):
            for variable in settled:
                session.variables.pop(variable, None)
            self.set_named(parameters.get("newname", []), session)

    @staticmethod
    def set_named(names: list[Word], session: Session) -> None:
        for word in names:
            if word is not None:
                session.variables[folded(word)] = None


def may_name_tracked(word: Mapping[str, Any]) -> bool:
    """Whether a provider path or name the reader reports, and the text does not settle, may name a variable git reads.

    It may unless the part after its last separator is literal text naming none: `"$dir\\out.txt"` names the variable
    out.txt wherever $dir points, and nothing else on the environment provider. Every expansion the reader reports
    begins with $, so a last part that holds one holds a $."""
    text = str(word.get("text") or "")
    last = re.split(r"[\\/]", text)[-1]
    if not last or re.search(r"[$`*?\[\]:]", last):
        return True
    return is_tracked(last)


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


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int, variables: Variables) -> None:
    """Reads a PowerShell command with PowerShell's own parser, started only for text it could refuse.

    Starting pwsh costs far more than the guard's git calls, so text may_refuse passes never pays for it. A location
    belongs to the session, not to a script block's scope, so only a child pwsh restores one.
    """
    if not may_refuse(command, cwd, reading):
        return
    events = powershell_events(command, reading, depth)
    if events is None:
        return
    # A session inherits the environment the hook was given; the tracked variables are those its starter passes on.
    inherited = {name: value for name, value in {**reading.environment, **variables}.items() if not is_tracked(name)}
    environment = {folded(name): value for name, value in inherited.items()}
    session = Session(cwd, {**environment, **git_variables(variables, reading.notes)})
    PowerShellReader(reading, depth).run(event_tree(events), session)


ShellReader = Callable[[str, "Path | None", Reading, int, Variables], None]
# Monitor's command runs in the shell the Bash tool uses, so the guard reads it as Bash.
SHELL_READERS: dict[str, ShellReader] = {"Bash": read_bash, "Monitor": read_bash, "PowerShell": read_powershell}


def read_command(tool: str, command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """Every git call a shell tool's command makes, with where each acts, and what could not be read."""
    reading = start_reading(command, cwd, environment)
    SHELL_READERS[tool](command, cwd, reading, 0, inherited_variables(environment))
    return reading


def start_reading(command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """A reading of one command, with the guarded hubs in view of the session, before any of it is read."""
    guarded_hub_at.cache_clear()
    guarded_hub_of_repository.cache_clear()
    return Reading(environment, command, context_hubs=hubs_in_view(cwd, environment))


def reached_hubs(reading: Reading) -> set[Path]:
    """The guarded hubs a reading's settled places name."""
    found: set[Path] = set()
    for call in reading.calls:
        hub, _ = reached_hub(call.place)
        if hub is not None:
            found.add(hub)
    return found


# ---------------------------------------------------------------------------------------------------------------
# The hook


def guard(stream: str) -> int:
    # The command and the guarded hubs in view, known before the command is read, so that a reader or a judgment that
    # fails leaves a command the guard cannot read; None until then.
    in_view: tuple[str, set[Path]] | None = None
    try:
        event = json.loads(stream)
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        cwd = Path(platform_support.from_shell_path(event.get("cwd") or str(Path.cwd())))
        refused: str | None = None
        if tool in FILE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not isinstance(target, str):
                print(f"hub guard skipped: the {tool} event names no file path", file=sys.stderr)
            elif hub_protected(resolve(target, cwd)):
                refused = DENY_REASON
        elif tool in SHELL_READERS and not isinstance(tool_input.get("command"), str):
            print(f"hub guard skipped: the {tool} event names no command", file=sys.stderr)
        elif tool in SHELL_READERS:
            reading = start_reading(tool_input["command"], cwd, os.environ)
            hubs = set(reading.context_hubs)
            in_view = (reading.text, hubs)
            SHELL_READERS[tool](reading.text, cwd, reading, 0, inherited_variables(os.environ))
            hubs |= reached_hubs(reading)
            reason = denied(reading, hubs)
            refused = None if reason is None else f"{reason} {DENY_REASON}"
            for note in reading.notes:
                print(f"hub guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not block every tool; the regression suite pins what it refuses
        if in_view is None or not (in_view[1] and mentions(MENTIONS_GIT, in_view[0])):
            print(f"hub guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        # Where a guarded hub is in view, a command naming git that the guard failed to read or judge is one it cannot
        # read, whether a reader fails on nesting past Python's recursion limit or on any other error.
        refused = (
            f"The guard failed reading this command ({type(exc).__name__}: {exc}), and it names git, so it is refused "
            f"while a hub is guarded. Rewrite it so each git command is written out. {DENY_REASON}"
        )
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


def main() -> int:
    return guard(sys.stdin.read())


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")

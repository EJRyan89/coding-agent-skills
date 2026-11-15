"""The hub guard: the Claude Code PreToolUse hook .claude/settings.json runs, which reads the hook JSON on stdin.

Usage:
  python tools/hub_guard.py   reads one PreToolUse event on stdin and prints a deny decision, or nothing

The guard refuses file edits inside the hub and every git command that acts on the hub but the read-only forms
READ_ONLY_FORMS declares, whether they run through the Bash tool, the PowerShell tool, or the Monitor tool, whose
command runs in Bash. It fails
closed: it reads a command only as far as it understands it, Bash by tools/bash_syntax.py's grammar and PowerShell by
PowerShell's own parser (tools/hub_guard-powershell.ps1), judges every git call it can place, and refuses every part
it does not understand while a guarded hub is in view. What it understands is the tables below, each a catalogue a
shell publishes with every entry understood or in NOT_READ, and docs/parallel-sessions.md ("The guard's contract")
states the contract. Every decision is made here. It finds the checkout that holds a path, and runs git, through
tools/worktrees.py, which owns the hub and its worktrees. It is inert unless `git config coding-agent-skills.hubGuard
true` was set in this clone.

Exit status:
  0  the hook answered: a deny decision printed, or nothing, which allows the call
  1  a last line FAILED <reason>
"""

from __future__ import annotations

import base64
import contextlib
import functools
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import executable_locator
import file_presence
import hook_decision
import json_reader
import json_writer
import whole_number
from bounded_process import run_bounded
from console import run_main
from flat_text import flat_text
from git_client import (
    CARRIER_OPTIONS,
    CONFIG_READ_ACTIONS,
    CONFIG_READ_OPTIONS,
    CONFIG_VALUE_OPTIONS,
    PLAIN_ACTIONS,
    CarrierOptions,
    GitError,
    GitResult,
    carried_option,
    failure_message,
    output_line,
    output_lines,
)
from git_client import GLOBAL_VALUE_OPTIONS as GIT_OPTIONS_WITH_VALUE
from job_tree import process_tree
from option_reader import (
    MACOS_ENV_LETTERS,
    NO_VALUE,
    OptionReading,
    OptionTable,
    Word,
    next_word,
    option_refusal,
    option_table,
    slash_kind,
)
from path_keys import name_key
from text_lines import text_lines

from deployer import platform_support
from tools import bash_syntax, worktrees
from tools.worktrees import CLIENT, GUARD_GIT_TIMEOUT_SECONDS, MAIN, Checkout, same_path

GUARD_SETTING = "coding-agent-skills.hubGuard"
FILE_TOOLS = {"Edit", "NotebookEdit", "Write"}
# The options of git's subcommands that write a file at a path they are given, read in every spelling git's option
# parser takes: a long option exactly, with its value after = or as the next word, or abbreviated to any prefix of its
# name; a short one alone, with its value attached, or in a bundle. Each written path is judged as a redirection's
# target is (read_written, hub_protected), from where git starts. GIT_OUTPUT is read in every subcommand, since each
# that reads git's diff or revision options takes it (GIT_OUTPUT_SUBCOMMANDS); config's --file is judged by
# config_refusal. A root, under which git writes the files it checks out or patches (checkout-index's --prefix, the
# --directory of am and apply), is judged the same way: a root in a hub's tracked surface writes there.
# `tests/tools/test_hub_guard_git_options.py` holds every option `git <subcommand> -h` lists with a path for its value,
# or named as a root, to one of these tables or GIT_PATH_OPTIONS_READ.
GIT_OUTPUT = "--output"
GIT_WRITING_OPTIONS: dict[str, frozenset[str]] = {
    "am": frozenset({"--directory"}),
    "apply": frozenset({"--build-fake-ancestor", "--directory"}),
    "archive": frozenset({"-o"}),
    "bugreport": frozenset({"-o", "--output-directory"}),
    "checkout--worker": frozenset({"--prefix"}),
    "checkout-index": frozenset({"--prefix"}),
    "clone": frozenset({"--separate-git-dir"}),
    "credential-cache": frozenset({"--socket"}),
    "credential-store": frozenset({"--file"}),
    "diagnose": frozenset({"-o", "--output-directory"}),
    "fast-export": frozenset({"--export-marks"}),
    "fast-import": frozenset({"--export-marks"}),
    "format-patch": frozenset({"-o", "--output-directory"}),
    "gc": frozenset({"--expire-to"}),
    "index-pack": frozenset({"-o"}),
    "init": frozenset({"--separate-git-dir"}),
    "init-db": frozenset({"--separate-git-dir"}),
    "mailsplit": frozenset({"-o"}),
    "read-tree": frozenset({"--index-output"}),
    "repack": frozenset({"--expire-to", "--filter-to"}),
}
# The subcommands that write a file in the directory git starts in when no option or operand names another: a patch
# per commit, a report, a diagnostics archive, a blob's temporary copy, a repository, and a clone's directory, named
# after its repository. format-patch's --stdout writes none.
GIT_DEFAULT_WRITES = frozenset({"bugreport", "clone", "diagnose", "format-patch", "init", "init-db", "unpack-file"})


PRESENCE_ALLOWED = {
    "moved_to": (
        "a target it cannot stat leaves both places open, as one that is no directory does, so a failed stat never "
        "settles where the shell is"
    ),
}


@dataclass(frozen=True)
class OperandWriter:
    """How a git subcommand writes a file at a path its operands name, read by operand_writes.

    first is the first of its operands it writes, and count how many from there, None for every one after it,
    counted after its own subcommand's word where the table's key names one; valued are the options that take the
    next word as their value, so that word is no operand; modes are the boolean options of which the last given must
    be on for it to write at all, as an in-place mode; and stdout are the options with which it writes no operand,
    printing instead. An empty modes writes without one."""

    first: int = 0
    count: int | None = None
    valued: frozenset[str] = frozenset()
    modes: frozenset[str] = frozenset()
    stdout: frozenset[str] = frozenset()


# The git subcommands that write a file at a path an operand names, each keyed by its name, or by its name and the
# word of its own subcommand that writes: pack-objects a pack beside its base name, index-pack an index beside its pack,
# init a repository in its directory, clone its directory after the repository it copies, bundle create its bundle,
# worktree add the new worktree and worktree move the place it moves one to, mailinfo the message and the patch it
# splits a mail into, merge-file the merge into its first file unless it prints it or merges objects, and
# interpret-trailers each file it is given in its in-place mode. Each written operand is judged as a redirection's
# target is (read_written, hub_protected), from where git starts. `tests/tools/test_hub_guard_git_options.py` holds
# each writer's valued options to the ones its `-h` lists, and every operand its usage names with a path and every
# boolean option its `-h` says writes to a row here or to GIT_OPERANDS_READ or GIT_MODES_READ with a reason.
GIT_OPERAND_WRITERS: dict[str, OperandWriter] = {
    "bundle create": OperandWriter(count=1, valued=frozenset({"--version"})),
    "clone": OperandWriter(
        first=1,
        valued=frozenset(
            {
                *("-b", "--branch", "--bundle-uri", "-c", "--config", "--depth", "--filter", "-j", "--jobs", "-o"),
                *("--origin", "--ref-format", "--reference", "--reference-if-able", "--revision"),
                *("--separate-git-dir", "--server-option", "--shallow-exclude", "--shallow-since", "--template"),
                *("-u", "--upload-pack"),
            }
        ),
    ),
    "index-pack": OperandWriter(valued=frozenset({"-o"})),
    "init": OperandWriter(
        valued=frozenset(
            {"-b", "--initial-branch", "--object-format", "--ref-format", "--separate-git-dir", "--template"}
        )
    ),
    "init-db": OperandWriter(
        valued=frozenset(
            {"-b", "--initial-branch", "--object-format", "--ref-format", "--separate-git-dir", "--template"}
        )
    ),
    "interpret-trailers": OperandWriter(
        valued=frozenset({"--if-exists", "--if-missing", "--trailer", "--where"}), modes=frozenset({"--in-place"})
    ),
    "mailinfo": OperandWriter(valued=frozenset({"--encoding", "--quoted-cr"})),
    "merge-file": OperandWriter(
        count=1,
        valued=frozenset({"-L", "--diff-algorithm", "--marker-size"}),
        stdout=frozenset({"-p", "--stdout", "--object-id"}),
    ),
    "pack-objects": OperandWriter(
        valued=frozenset(
            {
                *("--compression", "--depth", "--filter", "--index-version", "--keep-pack", "--max-pack-size"),
                *("--missing", "--name-hash-version", "--threads", "--uri-protocol", "--window", "--window-memory"),
            }
        ),
        stdout=frozenset({"--stdout"}),
    ),
    "worktree add": OperandWriter(count=1, valued=frozenset({"-b", "-B", "--reason"})),
    "worktree move": OperandWriter(first=1, count=1),
}
GIT_OPERAND_SUBCOMMANDS = frozenset(key.split(" ")[0] for key in GIT_OPERAND_WRITERS)
# The subcommands that write a file at a path a file they read names, in a mode their boolean option turns on, so the
# path is one the text does not settle: apply's --unsafe-paths lets a patch's own paths lie outside the work tree. Each
# row is read as a GIT_OPERAND_WRITERS row's modes are, by its modes and its options with a value, so a value spelled
# as the mode's --no- form is a value; it writes no operand.
GIT_UNREAD_MODES: dict[str, OperandWriter] = {
    "apply": OperandWriter(
        count=0,
        valued=frozenset({"--exclude", "--include", "--whitespace", "-C", "-p"}),
        modes=frozenset({"--unsafe-paths"}),
    ),
}
# The operands a subcommand's usage names with a path that it does not write outside the checkout it runs in: each is
# read, names a path in the repository, or is written only in the work tree of the checkout it runs in, which
# READ_ONLY_FORMS judges in a hub, whose git refuses a path outside it. A subcommand GIT_OPERAND_WRITERS names is held
# to its row there instead.
GIT_OPERANDS_READ: dict[str, str] = {
    **dict.fromkeys(
        ("am <mbox>", "am <Maildir>", "apply <patch>", "mailsplit <mbox>", "mailsplit <Maildir>"),
        "reads the mail or the patch",
    ),
    **dict.fromkeys(("bisect <logfile>", "hash-object <file>", "pack-redundant <pack-filename>"), "reads the file"),
    "fetch-pack <directory>": "names the repository it fetches from",
    "send-pack <directory>": "names the repository whose refs and objects it updates",
    "config <file-option>": "is judged by config_refusal",
    **dict.fromkeys(
        (
            *("add <pathspec>", "annotate <file>", "archive <path>", "bisect <pathspec>", "blame <file>"),
            *("cat-file <path|tree-ish>", "check-attr <pathname>", "check-ignore <pathname>", "checkout <file>"),
            *("checkout-index <file>", "clean <pathspec>", "commit <pathspec>", "diff <path>", "diff <pathspec>"),
            *("diff-files <path>", "diff-index <path>", "diff-tree <path>", "difftool <path>", "fast-export <path>"),
            *("grep <path>", "history <pathspec>", "last-modified <pathspec>", "log <path>", "ls-files <file>"),
            *("ls-tree <path>", "merge-index <filename>", "pickaxe <file>", "rerere <pathspec>", "reset <pathspec>"),
            *("rev-list <path>", "rm <pathspec>", "shortlog <path>", "show <path>", "stage <pathspec>"),
            *("stash <pathspec>", "status <pathspec>", "update-index <file>", "whatchanged <path>"),
        ),
        "names paths in the repository or in the work tree it runs in",
    ),
    **dict.fromkeys(
        ("mv <destination>", "mv <destination-directory>"),
        "moves within the work tree it runs in, and git refuses a destination outside it",
    ),
}
# The boolean options a subcommand's `-h` says write or overwrite that write nowhere a path names: in the work tree or
# the git directory of the repository the call runs in, which READ_ONLY_FORMS judges in a hub, or only refs or
# objects.
GIT_MODES_READ: dict[str, str] = {
    **dict.fromkeys(
        ("checkout-index -f", "checkout-index --force", "checkout-index --temp"),
        "writes in the work tree it runs in, or beside the prefix GIT_WRITING_OPTIONS judges",
    ),
    **dict.fromkeys(("fetch -f", "fetch --force", "pull -f", "pull --force"), "overwrites a ref"),
    **dict.fromkeys(("fsck --lost-found", "fsck-objects --lost-found"), "writes in the git directory it runs in"),
    "hash-object -w": "writes an object",
}
# The options of git's subcommands that start a command or a program their value names, which the guard does not
# read and which may run git: refused while a hub is guarded, as a runner whose command may be git is (RUNNERS).
# ejryan89-skill-core's git_client.CARRIER_OPTIONS is the catalogue its plain client refuses; the guard adds the
# subcommands that client never runs at all, so its catalogue leaves them out: difftool's command, filter-branch's
# shell filters, and init-db's template, whose hooks a later git call runs. A bisect or submodule action outside
# git_client.PLAIN_ACTIONS, `bisect run` and `submodule foreach` among them, runs a command too.
GIT_RUNNING_OPTIONS: dict[str, CarrierOptions] = {
    **CARRIER_OPTIONS,
    "difftool": CarrierOptions(frozenset({"extcmd"}), frozenset("x")),
    "filter-branch": CarrierOptions(
        frozenset(
            {"setup", "env-filter", "tree-filter", "index-filter", "parent-filter", "msg-filter", "commit-filter"}
            | {"tag-name-filter"}
        )
    ),
    "init-db": CarrierOptions(frozenset({"template"})),
}
# The subcommands that read git's diff or revision options, among them GIT_OUTPUT, found by running each builtin with
# it: a word of theirs the text does not settle may be that option, so one is refused while a hub is guarded, as it
# is for every subcommand the tables above name.
GIT_OUTPUT_SUBCOMMANDS = frozenset(
    {
        *("annotate", "archive", "backfill", "blame", "bundle", "cherry-pick", "diff", "diff-files", "diff-index"),
        *("diff-pairs", "diff-tree", "fast-export", "format-patch", "last-modified", "log", "pickaxe", "range-diff"),
        *("reflog", "rev-list", "revert", "shortlog", "show", "stash", "whatchanged"),
    }
)
# The options `git <subcommand> -h` lists with a path for its value that neither write a file nor run a command: each
# reads the file, or names a place in the repository rather than a file git writes.
GIT_PATH_OPTIONS_READ: dict[str, str] = {
    **dict.fromkeys(
        (
            *("add --pathspec-from-file", "checkout --pathspec-from-file", "commit --pathspec-from-file"),
            *("reset --pathspec-from-file", "restore --pathspec-from-file", "rm --pathspec-from-file"),
            "stage --pathspec-from-file",
        ),
        "reads the paths to act on from the file",
    ),
    **dict.fromkeys(
        ("am --exclude", "am --include", "apply --exclude", "apply --include"), "names the paths a patch applies to"
    ),
    **dict.fromkeys(
        (
            *("annotate --ignore-revs-file", "blame --ignore-revs-file", "pickaxe --ignore-revs-file"),
            *("annotate -S", "blame -S", "pickaxe -S", "annotate --contents", "blame --contents"),
            "pickaxe --contents",
        ),
        "reads the revisions or contents to blame from the file",
    ),
    "archive --add-file": "reads the file into the archive",
    "archive --prefix": "names a directory inside the archive",
    "write-tree --prefix": "names a directory of the index whose tree it writes as an object",
    "archive --add-virtual-file": "adds the content its value gives to the archive",
    "check-mailmap --mailmap-file": "reads the mailmap from the file",
    **dict.fromkeys(("clone --recurse-submodules", "clone --recursive"), "names the submodules to clone, by pathspec"),
    **dict.fromkeys(
        (
            *("commit -F", "commit --file", "commit -t", "commit --template", "commit-tree -F", "fmt-merge-msg -F"),
            *("fmt-merge-msg --file", "merge -F", "merge --file", "tag -F", "tag --file"),
        ),
        "reads a message from the file",
    ),
    **dict.fromkeys(
        ("commit-graph --object-dir", "multi-pack-index --object-dir"),
        "names the object store to read and write, which holds only objects and their indexes",
    ),
    **dict.fromkeys(
        (
            *("diff -O", "diff-files -O", "diff-index -O", "diff-pairs -O", "diff-tree -O", "range-diff -O"),
            *("diff --rotate-to", "diff --skip-to", "diff-pairs --rotate-to", "diff-pairs --skip-to"),
            *("range-diff --rotate-to", "range-diff --skip-to"),
        ),
        "reads the order of the paths it shows, or names a path to start at",
    ),
    **dict.fromkeys(
        ("fast-export --import-marks", "fast-export --import-marks-if-exists"), "reads marks from the file"
    ),
    **dict.fromkeys(
        ("fast-import --import-marks", "fast-import --import-marks-if-exists"), "reads marks from the file"
    ),
    **dict.fromkeys(
        ("fast-import --rewrite-submodules-from", "fast-import --rewrite-submodules-to"), "reads a marks file"
    ),
    **dict.fromkeys(("format-patch --description-file", "format-patch --signature-file"), "reads text for the patches"),
    "grep -f": "reads patterns from the file",
    "hash-object --path": "names the path whose attributes apply",
    **dict.fromkeys(("log -L", "show -L", "whatchanged -L"), "names a range of lines of a tracked file"),
    **dict.fromkeys(
        ("ls-files -X", "ls-files --exclude-from", "ls-files --exclude-per-directory"),
        "reads exclude patterns from the file",
    ),
    "read-tree --prefix": "names a directory inside the index",
}
# The settings a command line may hand git with -c or --config-env while a hub is guarded: the ones the guard reads
# itself (core.worktree, alias.<name>), and ones that change only how git prints, who a commit is by, or which
# repositories it trusts. Every other setting is refused, since many run a command (core.pager, pager.<command>,
# core.fsmonitor, core.sshCommand, diff.external, credential.helper, a filter's command) or choose where git writes
# (core.hooksPath, format.outputDirectory).
GIT_SETTINGS_UNDERSTOOD = frozenset(
    {
        *("core.worktree", "core.quotepath", "core.autocrlf", "core.safecrlf", "core.eol", "core.abbrev"),
        *("core.longpaths", "core.filemode", "core.ignorecase", "core.symlinks", "user.name", "user.email"),
        *("author.name", "author.email", "committer.name", "committer.email", "safe.directory"),
        *("init.defaultbranch", "log.date", "log.decorate", "log.abbrevcommit", "log.showroot", "log.follow"),
        *("log.mailmap", "diff.renames", "diff.algorithm", "diff.noprefix", "diff.mnemonicprefix", "diff.context"),
        *("diff.interhunkcontext", "diff.relative", "diff.statgraphwidth", "gc.auto", "maintenance.auto"),
        *("pull.rebase", "pull.ff", "fetch.prune", "fetch.prunetags", "fetch.parallel", "rebase.autosquash"),
        *("rebase.autostash", "versionsort.suffix", "tag.sort", "branch.sort", "status.showuntrackedfiles"),
        *("commit.gpgsign", "tag.gpgsign", "protocol.version"),
    }
)
# The sections every one of whose settings a command line may hand git: aliases, which the guard reads, and colors,
# advice, columns, and encodings, which change only how git prints.
GIT_SECTIONS_UNDERSTOOD = frozenset({"alias", "color", "advice", "column", "i18n"})
# git config's forms that only read: an action that reads, git_client's CONFIG_READ_OPTIONS, or a subcommand that does,
# its CONFIG_READ_ACTIONS, and beside it only the options that change what is read or how it prints, these and the
# ones that take a value as the next word or after =, git_client's CONFIG_VALUE_OPTIONS and -f. Any other option, an
# abbreviation included, may write, so a git config holding one is judged as one that writes (config_refusal).
CONFIG_MODIFIERS = frozenset(
    {
        *("--global", "--system", "--local", "--worktree", "--bool", "--int", "--bool-or-int", "--path"),
        *("--expiry-date", "-z", "--null", "--name-only", "--show-origin", "--show-scope", "--includes"),
        *("--no-includes", "--fixed-value", "--all", "--regexp", "--no-type"),
    }
)
CONFIG_SCOPES = frozenset({"--global", "--system", "--local", "--worktree"})
CONFIG_VALUED = frozenset({"-f", *(f"--{name}" for name in CONFIG_VALUE_OPTIONS)})
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
# The variables that choose the repository, work tree, or index file whose HEAD, index, and files a git command writes;
# index_reached judges the index GIT_INDEX_FILE names. GIT_COMMON_DIR is not one: it moves the store of objects,
# configuration, and refs every worktree shares, never a checkout's HEAD, index, or tree; STORE_VARIABLES judges it.
LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
# The inherited variables that name the store a git call writes its objects into, by what git writes there; each is
# judged by store_reached, against the call's own store and the guarded hubs (Judge.environment_refusal).
STORE_VARIABLES = {
    "GIT_OBJECT_DIRECTORY": "objects",
    "GIT_COMMON_DIR": "objects, configuration, and shared refs",
}
# The inherited variables that change which objects git takes its store to hold, or which parents it takes a commit to
# have, set even to an empty value, by what each does: a call that writes the refs a guarded hub's store holds may move
# them to objects that store lacks, or walk past objects it then prunes (Judge.environment_refusal).
OBJECT_VIEW_VARIABLES = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "names further stores git reads objects from",
    "GIT_SHALLOW_FILE": "names the commits git takes as having no parents, which a fetch tells the server it has",
    "GIT_GRAFT_FILE": "names parents git takes commits to have in place of their own",
    "GIT_REPLACE_REF_BASE": "names the refs git reads replacement objects from",
}
# git appends a trace to the absolute path any GIT_TRACE variable names, and trace2 makes a file inside one that is a
# directory (trace.c, trace2/tr2_dst.c); a relative path, a descriptor, a socket, and a switch write no file.
TRACE_VARIABLE = re.compile(r"GIT_TRACE[A-Z0-9_]*")
# trace2 also reads its three targets from the system and the global configuration, include.path followed, but never
# from a repository's or the command line's, and the variable of the same name outranks a setting whenever it holds a
# value (trace2/tr2_sysenv.c, read_very_early_config); the value 0 turns a target off.
TRACE2_SETTINGS = {
    "trace2.normaltarget": "GIT_TRACE2",
    "trace2.perftarget": "GIT_TRACE2_PERF",
    "trace2.eventtarget": "GIT_TRACE2_EVENT",
}
TRACE2_SCOPES = frozenset({"system", "global"})
# The other settings that name a file git writes, which git reads from every scope as a path (git_config_pathname): the
# cookie jar http.cookieFile or http.<url>.cookieFile names, which an https call writes back while an http.saveCookies
# or http.<url>.saveCookies setting is on, and the folder fsmonitor.socketDir names, where git's own file-system monitor
# makes its socket.
COOKIE_FILE = re.compile(r"http\.(?:.+\.)?cookiefile", re.DOTALL)
SAVE_COOKIES = re.compile(r"http\.(?:.+\.)?savecookies", re.DOTALL)
SOCKET_DIRECTORY = "fsmonitor.socketdir"
# The values git reads a boolean setting as false by; any other, a setting with no value among them, is on.
FALSE_SETTINGS = frozenset({"false", "no", "off", "0", ""})
# The setting names configured_written_targets asks git for, as one extended regular expression.
CONFIGURED_WRITES = (
    "^("
    + "|".join(re.escape(setting) for setting in TRACE2_SETTINGS)
    + r"|http\.(.+\.)?(cookiefile|savecookies)|fsmonitor\.socketdir)$"
)
# libcurl, which git's https transport runs, appends the keys of each TLS session to the file this variable names,
# opening a relative path from where it starts; so do other programs a command may start.
KEY_LOG_VARIABLE = "SSLKEYLOGFILE"
# The settings that name a further configuration file git reads, by their name as git lists it (section and name in
# lower case, a condition as written).
# The variables that locate the system and global configuration files, each read as a path git opens.
CONFIGURATION_LOCATIONS = ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL", "HOME", "XDG_CONFIG_HOME")
INCLUDE_SETTING = re.compile(r"include\.path|includeif\..+\.path", re.DOTALL)
# git for Windows sends its output to the file these name, read from where it starts (compat/mingw.c), but for the
# values that name none; each is judged on every platform, since one git may be either build.
REDIRECT_VARIABLES = {"GIT_REDIRECT_STDOUT": frozenset({"off"}), "GIT_REDIRECT_STDERR": frozenset({"off", "2>&1"})}
# The variables that carry command-line configuration, which git reads for core.worktree and alias.<name>.
CONFIGURATION_VARIABLES = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
CONFIGURATION_PAIR = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+")
# The environment variable by which Bash hands a shell it starts a function exported with export -f.
EXPORTED_FUNCTION = re.compile(r"^BASH_FUNC_(.+)%%$")

# ---------------------------------------------------------------------------------------------------------------
# What the readers understand
#
# The guard reads a command only as far as it understands it, and refuses the rest while a guarded hub is in view
# ("The guard's contract" in docs/parallel-sessions.md). Each table below is one catalogue the shells publish, and
# every entry is either understood, with how, or in NOT_READ, with why the guard does not read it. The guard's suites
# generate their rows from the catalogue each shell lists itself, `compgen -b`, `compgen -k`, `set -o`, `shopt`, and
# `compgen -v` from Bash and the AST node types by reflection from PowerShell, and fail on an entry neither table holds.

# The programs that run another command their words name, by the catalogue the guard refuses. While a hub is guarded,
# each is refused where the command it runs is or may be git, or where it is given an option RUNNER_OPTIONS does not
# know to be safe (runner_refusal); a runner around any other command is understood. Every other program a literal
# name starts is read by its words, unless READ_ONLY_PROGRAMS names it (read_unlisted): what it does inside itself, in
# its own language (a Python script, an awk program, a test suite), the guard does not see.
RUNNERS: dict[str, str] = {
    "bash": "a shell, whose script is read only as `-c` with a literal script, and refused where it runs git",
    "sh": "Bash in POSIX mode, or dash, either of which keeps an assignment made before a special builtin after it, "
    "which the guard does not follow",
    "pwsh": "PowerShell, whose script is read only as `-Command` or `-c` with one literal script, after -NoProfile, "
    "-NonInteractive, or -NoLogo, and refused where it runs git",
    "powershell": "Windows PowerShell, whose parser differs from pwsh's, so the guard does not read its script",
    **dict.fromkeys(
        ("zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "busybox"), "a shell whose script the guard does not read"
    ),
    "cmd": "Windows' command interpreter, whose script the guard does not read",
    "wsl": "runs a command in a Linux distribution, which the guard does not read",
    "env": "runs its command after the assignments before it, or, with -S, a command it splits from a string",
    "xargs": "runs its command with words from its input, or, with -I, -i, or --replace, words it builds from them",
    **dict.fromkeys(
        ("timeout", "nohup", "nice", "stdbuf", "ionice", "chrt", "taskset", "setsid", "unbuffer", "chronic"),
        "runs the command its words name",
    ),
    **dict.fromkeys(("strace", "ltrace", "time", "chroot", "sudo", "doas"), "runs the command its words name"),
    **dict.fromkeys(
        ("su", "runuser", "ssh", "script", "watch", "parallel", "flock"),
        "runs a command line it is handed as a string, which may be any command",
    ),
}
# The runners refused whatever they run: a shell whose script the guard does not read, and a runner that runs a
# command line it is handed as a string, whose words no reading of its own words settles.
RUNNERS_ALWAYS_REFUSED = frozenset(
    {
        *("sh", "powershell", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "busybox", "cmd", "wsl"),
        *("su", "runuser", "ssh", "script", "watch", "parallel", "flock"),
    }
)


# The options of each runner the guard reads, by an allow-list: the ones known neither to build its command from text,
# as xargs's -I and env's -S do, nor to change where it runs, as env's -C does, nor to write a file or run another
# program, nor to change what runs. The Rust coreutils' env takes two that may: -f or --file sets variables from a
# file the text does not show, GIT_DIR or PATH among them, and -a or --argv0 names what its command runs as, by which a
# multicall program, as the Rust coreutils' `coreutils` and busybox are, picks the program it runs; neither is safe.
# Each runner's help and version options run nothing, so no command runs after one, and are left out too. While a hub
# is guarded every other option word of a runner is refused, in any spelling: exactly, with its value attached,
# abbreviated, or inside a bundle (option_refusal). A long option is understood only spelled in full, since an
# abbreviation of a safe option may be one of another option too. timeout's -f and -p, the Rust coreutils' short
# forms of --foreground and --preserve-status, are read as those are. env's NAME=VALUE words are read as assignments
# for its command (runner_assignments). macOS's env, FreeBSD's, lists `-` as a letter and takes no long option, so it
# reads a long option word as a bundle of letters, `--unset tar xf` as `-`, `-u nset`, and the command tar: env's
# table reads each dash word that way too, by that build's letters and the ones of them known to be safe, and refuses
# one the two builds read as taking different words (option_reader's dash_reading), so `--unset NAME` is refused and
# `--unset=NAME` understood. macOS's other runners take no `-` letter and refuse such a word, or, as its xargs, read
# GNU's long names. One NAME=VALUE word before any other runner's command, as sudo takes, is refused, since it may
# set a variable the guard does not judge. bash and pwsh are read by their scripts (shell_script), and the runners
# RUNNERS_ALWAYS_REFUSED names are refused whatever their options.
RUNNER_OPTIONS: dict[str, OptionTable] = {
    "env": option_table(
        "i0u:v",
        "ignore-environment null unset= debug",
        assignments=True,
        dash_build=MACOS_ENV_LETTERS,
        dash_safe="-0iuv",
    ),
    "xargs": option_table(
        "0a:d:E:L:n:P:rs:tx",
        "null arg-file= delimiter= max-args= max-procs= no-run-if-empty max-chars= verbose exit",
    ),
    "timeout": option_table("fk:ps:v", "kill-after= signal= verbose preserve-status foreground", operands=1),
    "nice": option_table("n:", "adjustment=", numbered=re.compile(r"-[+-]?[0-9]+")),
    "nohup": option_table(),
    "stdbuf": option_table("i:o:e:", "input= output= error="),
    "ionice": option_table("c:n:t", "class= classdata= ignore"),
    "chrt": option_table("bdfiorRv", "batch deadline fifo idle other rr reset-on-fork verbose", operands=1),
    "taskset": option_table("c", "cpu-list", operands=1),
    "setsid": option_table("cfw", "ctty fork wait"),
    "unbuffer": option_table("p"),
    "chronic": option_table("ev"),
    "strace": option_table("cfq"),
    "ltrace": option_table("cf"),
    "time": option_table("f:pqv", "format= portability quiet verbose"),
    "chroot": option_table(operands=1),
    "sudo": option_table("nu:g:HEk", "non-interactive user= group= set-home reset-timestamp"),
    "doas": option_table("nu:"),
}
# The options of the programs the guard understands that have options which run a command, write a file the command
# does not name, or change where the program acts, by an allow-list as RUNNER_OPTIONS's: find's -exec family and its
# -delete and -fprint family, rg's --pre and --hostname-bin, sort's --compress-program and --output, and tar's options
# that run a program, its extraction (-x, --extract, --get, and what only overwrites on extraction), and its -C, which
# writes the files an archive names in a directory the option chooses, and its -T, whose list of names may hand it
# options, are among the ones refused, and so is any word
# of such a program that the text does not settle. tar's archive named host:path, which tar opens through a remote
# shell, is refused unless --force-local is given (remote_archive). tar's letters are the ones GNU tar and bsdtar,
# Windows' tar.exe, either both read alike or only one of them knows: -H, -W, and -s, which one of them reads with a
# value and the other without, are refused. sort's and find's Windows builds read options written with a slash, by
# the slash allow-list each table holds, on Windows whatever the command's path (system_program).
PROGRAM_OPTIONS: dict[str, OptionTable] = {
    "find": option_table(
        "",
        " ".join(
            (
                "H L P not a and o or daystart follow nowarn warn regextype= depth mount noleaf xdev",
                "ignore_readdir_race noignore_readdir_race maxdepth= mindepth= files0-from=",
                "amin= anewer= atime= cmin= cnewer= context= ctime= empty false fstype= gid= group= ilname= iname=",
                "inum= ipath= iwholename= iregex= links= lname= mmin= mtime= name= newer= nouser nogroup path= perm=",
                "regex= readable writable executable samefile= wholename= size= true type= uid= used= user= xtype=",
                "print print0 printf= ls prune quit",
                *(f"newer{x}{y}=" for x in "aBcm" for y in "aBcmt"),
            )
        ),
        permutes=True,
        single_dash=True,
        # Windows' find.exe reads its lines and counts them; none of its options writes or runs anything.
        slash="V C N I OFF OFFLINE",
    ),
    "rg": option_table(
        "A:B:C:cd:e:E:f:Fg:HiIj:lLm:M:nNopPqr:sSt:T:uUvVwx0ab.",
        " ".join(
            (
                "after-context= before-context= context= count count-matches case-sensitive color= colors= column",
                "no-column context-separator= regexp= encoding= file= fixed-strings files files-with-matches",
                "files-without-match follow glob= iglob= glob-case-insensitive hidden no-hidden heading no-heading",
                "ignore-case ignore-file= json line-number no-line-number line-regexp max-columns= max-count=",
                "max-depth= max-filesize= multiline multiline-dotall no-config no-ignore no-ignore-vcs no-ignore-dot",
                "no-ignore-global no-ignore-parent no-ignore-exclude no-ignore-files no-messages null null-data",
                "only-matching passthru pcre2 pretty quiet replace= smart-case sort= sortr= stats text threads= trim",
                "type= type-not= type-list unrestricted vimgrep with-filename no-filename word-regexp invert-match",
                "byte-offset binary no-binary crlf",
            )
        ),
        permutes=True,
    ),
    "sort": option_table(
        "bdfgiMhnRrVcCk:msS:t:uz",
        " ".join(
            (
                "ignore-leading-blanks dictionary-order ignore-case general-numeric-sort ignore-nonprinting",
                "month-sort human-numeric-sort numeric-sort random-sort random-source= reverse sort= version-sort",
                "batch-size= check=? debug files0-from= key= merge stable buffer-size= field-separator= parallel=",
                "unique zero-terminated check-silent",
            )
        ),
        permutes=True,
        # Windows' sort.exe: /O writes its output to a file and /T keeps its working files in a directory, so neither.
        slash="R REVERSE + L= LOCALE= M= MEMORY= REC= RECORD_MAXIMUM=",
        # Whether sort.exe reads `-t` or `-temporary` as /T is not documented, so a dash word on Windows is held to the
        # slash options too, and `sort -u` is refused there. find.exe has no option that writes or runs anything.
        dash_switches=True,
    ),
    "tar": option_table(
        "AcdrtuvzjJZakmOpPhSBilnRof:X:b:K:N:V:",
        " ".join(
            (
                "catenate concatenate create diff compare append list update delete file= force-local",
                "exclude-from= exclude= verbose gzip gunzip ungzip bzip2 xz lzip lzma lzop zstd compress",
                "uncompress auto-compress no-auto-compress keep-old-files skip-old-files keep-newer-files",
                "no-overwrite-dir touch to-stdout preserve-permissions same-permissions",
                "no-same-permissions same-owner no-same-owner numeric-owner absolute-names dereference",
                "hard-dereference strip-components= wildcards no-wildcards wildcards-match-slash",
                "no-wildcards-match-slash anchored no-anchored ignore-case no-ignore-case exclude-vcs",
                "exclude-vcs-ignores exclude-backups exclude-caches exclude-caches-all exclude-caches-under recursion",
                "no-recursion null no-null unquote no-unquote verbatim-files-from no-verbatim-files-from format=",
                "posix old-archive portability owner= group= mode= mtime= sort= transform= xform= one-file-system",
                "ignore-zeros ignore-failed-read blocking-factor= record-size= read-full-records sparse full-time",
                "utc show-transformed-names show-stored-names show-omitted-dirs warning= quoting-style=",
                "check-links block-number label= newer= after-date= newer-mtime= starting-file= clamp-mtime",
                "delay-directory-restore no-delay-directory-restore acls no-acls xattrs no-xattrs selinux",
                "no-selinux verify exclude-tag= exclude-tag-all= exclude-tag-under= seek no-seek",
            )
        ),
        permutes=True,
        first_letters=True,
    ),
}
# The variables a program PROGRAM_OPTIONS names reads options from, as its words, or a file of them: tar's
# TAR_OPTIONS and TAPE, its default archive, which may name a remote one, and rg's configuration file. One assigned
# before the program, or in the environment, is refused, since it may hand the program an option its allow-list
# refuses.
PROGRAM_VARIABLES: dict[str, frozenset[str]] = {
    "tar": frozenset({"TAR_OPTIONS", "TAPE"}),
    "rg": frozenset({"RIPGREP_CONFIG_PATH"}),
}
# Each of them, refused where it is assigned before any runner, whatever the runner starts (read_runner_words).
PROGRAM_VARIABLE_NAMES = frozenset(name for names in PROGRAM_VARIABLES.values() for name in names)
# The runners that add words from their input to the command they run, which the text does not settle.
INPUT_RUNNERS = frozenset({"xargs"})
# The programs whose words name the files they write, create, remove, or change the mode or owner of, each with the
# option letters every build of it (GNU coreutils, uutils, BSD, busybox) reads as a flag, or None where no build gives
# a letter a value. Every word that may name such a file is judged as a redirection's target is (read_program_writes):
# each operand, a source among them, since telling a source from a target means reading every build's options; each
# option's value after `=`; and the rest of a bundle after its first letter that is no flag, which may be the value an
# option takes there, as cp's `-t<folder>`. dd writes only the file its `of=` operand names, and sed only with -i,
# when it rewrites each file it is given (written_words).
WRITING_PROGRAMS: dict[str, str | None] = {
    "cp": "abcdfHilLnNpPRrsTuvxXZ",
    "mv": "bfhinTuvZ",
    "install": "bcCdMpsUvZ",
    "tee": None,
    "touch": "acfhm",
    "truncate": "co",
    "rm": None,
    "rmdir": None,
    "unlink": None,
    "mkdir": "pvZ",
    "mkfifo": "Z",
    "mknod": "Z",
    "mktemp": "dqu",
    "chmod": None,
    "chown": None,
    "chgrp": None,
    "chflags": None,
    "chcon": "hHLPRv",
    "shred": "fuvxz",
    "dd": None,
    "sed": "abnrsuzE",
}
# The programs that make a link, through which a write later in the same command reaches a place the guard placed
# before the link existed, by what each makes. While a hub is guarded each is refused, and so are cp's and install's
# options that make one (LINK_OPTIONS); a link a program makes from inside itself is beyond the text.
LINK_PROGRAMS = {
    "ln": "makes a symbolic or a hard link",
    "link": "makes a hard link",
    "fsutil": "makes a hard link, or a file, among the actions its words choose",
}
# The options of a WRITING_PROGRAMS program that make it write a link where it would write a file: GNU and FreeBSD
# cp's -l and -s, GNU cp's --link and --symbolic-link, and BSD install's -l, by their letters and long names.
LINK_OPTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "cp": ("ls", ("link", "symbolic-link")),
    "install": ("l", ()),
}
# The multicall programs that run the utility their first word names, as the Rust coreutils' `coreutils cp` does and
# toybox's does: each is read as that utility, with the words after it (multicall). `--coreutils-prog=<name>` names the
# utility too.
MULTICALL_PROGRAMS = frozenset({"coreutils", "toybox"})
MULTICALL_OPTION = "--coreutils-prog="
# The names another build of a program the guard reads is installed under beside its own: macOS's bsdtar and gnutar.
BUILD_NAMES = {"bsdtar": "tar", "gnutar": "tar"}
# The programs known, in every build (GNU, uutils, BSD, busybox, Windows), to write no file their words name and to run
# no command, each with what it does instead: their words are not judged. While a hub is guarded every other program
# the tables above do not read is judged by its words (read_unlisted): refused where a word may name a file in a
# guarded hub or one holding it, or names git or a runner, or is a word the text does not settle. A program with an
# option that writes a file or runs a command, as `file -C`, `tree -o`, `less -o`, `xxd`'s second operand, `uniq`'s, or
# Windows' `expand.exe`'s destination,
# is left off, and so is one that reads its words as a program, as `awk` does. Each is known by its own name only, never
# under a prefix BUILD_PREFIXES names (built_name): a program installed as `ugrep` or `gdu` may be another program,
# as ugrep, whose `--save-config` writes a file, and gdu, whose `-o` does, are.
READ_ONLY_PROGRAMS: dict[str, str] = {
    **dict.fromkeys(
        ("cat", "head", "tail", "tac", "grep", "egrep", "fgrep", "findstr", "jq", "strings", "od", "hexdump"),
        "prints the files it is given, or what it finds in them",
    ),
    **dict.fromkeys(
        ("wc", "cut", "tr", "paste", "fold", "nl", "rev", "unexpand", "fmt", "column", "comm", "join"),
        "prints the files or the input it is given, reshaped or counted",
    ),
    **dict.fromkeys(("diff", "cmp"), "prints how the files it is given differ"),
    **dict.fromkeys(
        ("md5sum", "md5", "sha1sum", "sha256sum", "sha512sum", "shasum", "b2sum", "cksum"),
        "prints or checks the checksums of the files it is given",
    ),
    **dict.fromkeys(
        ("ls", "stat", "du", "df", "realpath", "readlink", "basename", "dirname", "pwd", "which", "where"),
        "prints facts about the paths it is given or finds",
    ),
    **dict.fromkeys(
        ("true", "false", "sleep", "seq", "yes", "expr", "date", "uname", "whoami", "id", "printenv", "nproc"),
        "prints, waits, or answers from its words alone",
    ),
}
# The names Python's interpreter is started by, and the option letters it reads before the script it runs: the flags,
# and the letters that take a value, attached or as the next word, whose values are judged as any unlisted program's
# words are, since `-X pycache_prefix=<folder>` writes there. A script TRUSTED_SCRIPTS names that a guarded hub tracks,
# run by such a name with only these options, is trusted whatever its own words (is_trusted_script). `-i`, which runs
# code from the input after the script, is not among the flags.
# The characters no Windows file name holds (path_value).
NO_FILE_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
PYTHON_NAMES = re.compile(r"python(?:3(?:\.[0-9]+)?)?")
PYTHON_FLAGS = "bBdEIOPqsSuvx"
PYTHON_VALUED = "WX"
# The hub's own tools a session runs from the hub, by their paths in it, each with why it writes no file a hub tracks
# whatever its words, but by the one write the guard allows from the hub itself, the fast-forward of main to origin's
# main, which a tool names in full as refs/remotes/origin/main, so no ref named origin/main redirects it: such a script
# is trusted whatever its words where a guarded hub tracks it (is_trusted_script), since the guard refuses every edit to
# a file a guarded hub tracks and every git call that writes it, so it runs what the hub's history holds. Every other
# script's words are judged as any unlisted program's, a hub tool that writes hub files when its words ask among them,
# as `tools/skill_reference.py --write` or `tools/new_skill.py <name>` does, and so is a script anywhere else, since a
# session's own tools may rewrite it. A tool joins only with a reason that holds for every word it reads.
TRUSTED_SCRIPTS: dict[str, str] = {
    "deploy.py": (
        "deploys into the home, or a throwaway one it holds inside the temporary directory, and writes its "
        "configuration, manifest, and journal there, never under its source, which the deployer suites' harness "
        "fails a test for changing"
    ),
    "tools/worktrees.py": (
        "makes a task's worktree and branch under the folder the hub ignores, surveys the worktrees, or runs this guard"
    ),
    "tools/project_release_sync.py": "changes only the GitHub project's fields, through gh",
    ".claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py": (
        "acts on the repository's pull requests on GitHub, through gh, and in the hub only fetches origin and "
        "fast-forwards main to refs/remotes/origin/main, named in full, the fast-forward the guard allows from the hub"
    ),
}
# The blanks before a # that make a line of the PowerShell reader a comment (powershell_reader): the ones its lines
# are indented with.
COMMENT_INDENT = " \t"
# gh's subcommands that write files into the directory gh runs in, or where their words name: each is judged as a
# program read_unlisted reads, and refused where it runs in a guarded hub.
GH_WRITERS = {
    ("run", "download"),
    ("release", "download"),
    ("attestation", "download"),
    ("repo", "clone"),
    ("gist", "clone"),
    ("codespace", "cp"),
}
# gh's subcommands that write a git configuration: the one of the repository gh runs in, which every worktree shares
# with its hub, as `repo set-default` does and `repo fork` and `repo create` do when they add a remote, or the global
# one, as `auth setup-git` and `auth login` do when they set git's credential helper, and `repo rename`, which rewrites
# the remote's URL there. Each is refused while a hub is guarded, wherever it runs.
GH_CONFIGURATION_WRITERS = {
    ("repo", "set-default"),
    ("repo", "rename"),
    ("repo", "fork"),
    ("repo", "create"),
    ("auth", "setup-git"),
    ("auth", "login"),
}
# The other names cobra gives a gh command or subcommand, by the name the tables above use.
GH_ALIASES = {"cs": "codespace", "at": "attestation"}
GH_SUBCOMMAND_ALIASES = {("repo", "new"): ("repo", "create")}
# sed rewrites each file it is given in place with -i, or BSD sed's -I, and with GNU sed's --in-place; the letters that
# take a value in every build end a bundle, and a letter neither flag nor one of these is read as -i. GNU sed's -l takes
# a value and BSD sed's is a flag, so it ends no bundle: BSD's `-li ''` rewrites in place.
SED_IN_PLACE = ("iI", "ef", "in-place")
# Each build of sed's options, by how it reads a bundle's letters: those that take no value, those that take one
# attached or as the next word, and those that take one only attached and may take none, as GNU's -i[SUFFIX]; a letter
# none of these names is one the build refuses. BSD sed reads no long option and stops at its first operand, GNU sed
# reads its long options by any prefix that names one alone and reads an option after an operand.
SED_BUILDS: dict[str, tuple[str, str, str]] = {"gnu": ("nrEsuzb", "efl", "i"), "bsd": ("Eanlru", "efiI", "")}
SED_LONG_OPTIONS = {
    **dict.fromkeys(("expression", "file", "line-length"), True),
    **dict.fromkeys(
        (
            *("quiet", "silent", "regexp-extended", "separate", "sandbox", "debug", "posix", "follow-symlinks"),
            *("unbuffered", "binary", "null-data", "zero-terminated", "help", "version", "in-place"),
        ),
        False,
    ),
}
# The sed commands that take nothing after them, those that take an optional count, and those whose argument runs to the
# end of the line (r and R read the file it names) or to a `;` (a label); a, i, and c take text to the end of the line,
# its lines joined by a backslash before each newline.
SED_PLAIN_COMMANDS = frozenset("=dDgGhHnNpPxzF{}")
SED_COUNTED_COMMANDS = frozenset("qQlL")
SED_LINE_COMMANDS = frozenset("rR")
SED_LABEL_COMMANDS = frozenset("btT:v")
SED_TEXT_COMMANDS = frozenset("aic")
# The flags an s command takes besides w, which writes the file the rest of the line names, and e, which runs the
# pattern space as a command.
SED_SUBSTITUTE_FLAGS = frozenset("gpiImM0123456789 \t")
# The prefixes a package manager installs a build of these programs, the runners, and the ones PROGRAM_OPTIONS names,
# under beside the system's own: Homebrew's GNU coreutils, findutils, tar, and sed take a g, as gcp, gtimeout, gfind,
# gtar, and gsed, and its uutils coreutils a u, as ucp (built_name).
BUILD_PREFIXES = ("g", "u")
# pwsh's options that change nothing the guard reads before -Command.
PWSH_QUIET = frozenset({"-noprofile", "-noninteractive", "-nologo"})
# The variables an assignment before git may give it, each with the values it may take, or none for any literal
# value: who a commit is by and when, an editor that edits nothing, and no prompt. Every other variable git reads
# may change where it acts, what configuration and aliases it reads, or what program it runs.
GIT_PREFIXES: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            *("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"),
            *("GIT_COMMITTER_DATE", "GIT_TERMINAL_PROMPT"),
        ),
        frozenset(),
    ),
    **dict.fromkeys(("GIT_EDITOR", "GIT_SEQUENCE_EDITOR"), frozenset({"true", ":"})),
    "GIT_PAGER": frozenset({"cat", ""}),
}
# gh runs git itself for these subcommands, which check a branch out in the checkout gh runs in.
GH_CHECKOUTS = {("pr", "checkout"), ("pr", "co"), ("repo", "sync")}
# The flags of gh read before its subcommand, each of which takes the next word as its value when written alone.
GH_VALUED_FLAGS = frozenset({"-R", "--repo", "--hostname"})
# The variables naming the system's temporary folder, whose value from the environment a redirection's target may
# take, as `> "$TMP/out.txt"` does.
TEMPORARY_VARIABLES = frozenset({"TMP", "TEMP", "TMPDIR"})
# Files a redirection may write that are no file of a checkout.
DEVICE_FILES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul", "NUL"})

# The Bash builtins the guard understands (`compgen -b` lists every one), with what each does that the guard reads.
# Every other builtin is in NOT_READ.
BUILTINS_UNDERSTOOD = {
    **dict.fromkeys((":", "true", "false"), "runs nothing"),
    **dict.fromkeys(("echo", "pwd", "type", "times", "help", "caller"), "prints, and changes nothing"),
    "jobs": "prints the jobs; -x, which runs the command its words name, is not read",
    "printf": "prints its words; -v, which assigns a variable, is not read",
    **dict.fromkeys(("test", "["), "tests its words; -v, which may evaluate a subscript as arithmetic, is not read"),
    "cd": "moves the shell to a directory, which places the git calls after it",
    "pushd": "moves the shell to a directory and keeps the one it left, which places the git calls after it",
    "popd": "moves the shell back to the directory pushd kept",
    **dict.fromkeys(("break", "continue", "exit", "return"), "ends a loop, a function, or the shell"),
    "shift": "renumbers the positional parameters",
    "wait": "waits for background jobs; an option, as -p, which assigns a variable, is not read",
    "kill": "sends a signal",
    **dict.fromkeys(("umask", "ulimit"), "sets the mode or the limits of what the shell starts"),
    "read": "assigns its input to names the guard needs no value of (a free name)",
    "command": "with -v or -V prints what a name is; run any other way it starts the name, which is not read",
    "set": "turns on or off the options SET_OPTIONS decides, or sets the positional parameters",
    "shopt": "turns on or off the options SHOPT_OPTIONS decides, or prints them",
}
# The Bash reserved words the guard understands (`compgen -k`); every other one is in NOT_READ. A compound command's
# words and bodies are read, a git call in one is placed only where no directory change in it may precede it, and the
# directory after one that changes it is unknown.
KEYWORDS_UNDERSTOOD = {
    **dict.fromkeys(("if", "then", "else", "elif", "fi"), "a conditional, whose bodies are read"),
    **dict.fromkeys(("case", "esac", "in"), "a case, whose word, patterns, and bodies are read"),
    **dict.fromkeys(("for", "select", "while", "until", "do", "done"), "a loop, whose words and bodies are read"),
    **dict.fromkeys(("{", "}"), "a group, whose body is read"),
    "!": "negates a pipeline's status",
    "time": "times a pipeline, which is read",
}
# set's options (`set -o`): each with the letter that names it in a bundle, or none, and the state in which the guard
# does not read a shell that holds it, "on" or "off", or "" where neither changes what it reads; NOT_READ says why
# for each that has one. An option the table does not name is not read either.
SET_OPTIONS: dict[str, tuple[str, str]] = {
    # Exports each later assignment, so a free name reaches every program's environment, git's among them where
    # names fold case.
    "allexport": ("a", "on"),
    # A brace expansion leaves a word unsettled, on or off.
    "braceexpand": ("B", ""),
    # Line editing, history, and job notices, which a command run as a script never uses.
    **dict.fromkeys(("emacs", "vi", "history", "ignoreeof", "nolog"), ("", "")),
    "histexpand": ("H", ""),
    "notify": ("b", ""),
    # Each ends the shell sooner, runs nothing, or fails a command the guard reads as running either way.
    "errexit": ("e", ""),
    "noexec": ("n", ""),
    "onecmd": ("t", ""),
    "nounset": ("u", ""),
    "noclobber": ("C", ""),
    "pipefail": ("", ""),
    # Traps, which the text cannot set, since trap is not read.
    "errtrace": ("E", ""),
    "functrace": ("T", ""),
    "hashall": ("h", ""),
    "monitor": ("m", ""),
    # A glob leaves a word unsettled, on or off.
    "noglob": ("f", ""),
    "verbose": ("v", ""),
    "keyword": ("k", "on"),
    "posix": ("", "on"),
    "physical": ("P", "on"),
    # Only how a nested bash starts, which the guard does not read.
    "privileged": ("p", ""),
    # Expands PS4 before each command, which the guard reads where the environment's PS4 holds no expansion.
    "xtrace": ("x", ""),
    "interactive-comments": ("", "off"),
    # Git Bash's, which drops a carriage return the guard reads as a blank.
    "igncr": ("", ""),
}
SET_LETTERS = {letter: name for name, (letter, _) in SET_OPTIONS.items() if letter}
# shopt's options (`shopt`): each with the state in which the guard does not read a shell that holds it.
SHOPT_OPTIONS: dict[str, str] = {
    "cdable_vars": "on",
    "lastpipe": "on",
    "cdspell": "on",
    **dict.fromkeys(("compat31", "compat32", "compat40", "compat41", "compat42", "compat43", "compat44"), "on"),
    "extdebug": "on",
    "interactive_comments": "off",
    # The rest change nothing the guard reads: globs and expansions leave a word unsettled whatever they do, the
    # builtins they change (local, the arrays, the subscripts) are not read, aliases the text cannot define, and
    # completion, history, prompts, jobs, mail, and output are an interactive shell's.
    **dict.fromkeys(
        (
            *("dotglob", "extglob", "failglob", "globasciiranges", "globskipdots", "globstar", "nocaseglob"),
            *("nullglob", "extquote", "patsub_replacement", "noexpand_translation", "expand_aliases", "sourcepath"),
            *("execfail", "inherit_errexit", "gnu_errfmt", "shift_verbose", "localvar_inherit", "localvar_unset"),
            *("nocasematch", "array_expand_once", "assoc_expand_once", "autocd", "checkhash", "checkjobs"),
            *("checkwinsize", "cmdhist", "complete_fullquote", "completion_strip_exe", "direxpand", "dirspell"),
            *("force_fignore", "histappend", "histreedit", "histverify", "hostcomplete", "huponexit", "lithist"),
            *("mailwarn", "no_empty_cmd_completion", "progcomp", "progcomp_alias", "promptvars", "varredir_close"),
            *("xpg_echo", "bash_source_fullpath", "login_shell", "restricted_shell"),
        ),
        "",
    ),
}
# The names beyond GIT_* that git reads to choose a file it reads or writes, or a program it runs, each with why; an
# assignment to one is never harmless (harmless_name). A name git reads only for its messages, as LANG or LC_ALL,
# chooses neither.
GIT_READ_NAMES: dict[str, str] = {
    "HOME": "chooses where git reads its global configuration, attributes, and ignore files",
    "XDG_CONFIG_HOME": "chooses where git reads its global configuration, attributes, and ignore files",
    "XDG_CACHE_HOME": "chooses where git's credential cache makes its socket",
    **dict.fromkeys(
        sorted(platform_support.GIT_WINDOWS_HOME_VARIABLES),
        "chooses git for Windows' home, where it reads its global configuration, when HOME is unset",
    ),
    "TMPDIR": "chooses where git writes the temporary files an external diff or a textconv reads",
    **dict.fromkeys(("TEMP", "TMP"), "chooses git for Windows' TMPDIR when that is unset"),
    "PAGER": "names the pager git runs when no setting or GIT_PAGER names one",
    **dict.fromkeys(("EDITOR", "VISUAL"), "names the editor git runs when no setting or GIT_EDITOR names one"),
    "SSH_ASKPASS": "names the program git runs to ask for a credential when GIT_ASKPASS is unset",
    KEY_LOG_VARIABLE: "names the file libcurl, which git's https transport runs, writes each TLS session's keys to",
}
# The prefixes of the names the dynamic loader reads as any program starts, git among them, which may load code into it.
LOADER_PREFIXES = ("LD_", "DYLD_")
# The variables Bash itself reads or sets, from its manual's "Shell Variables" and `compgen -v` in a bash started
# with no environment, each with "" where assigning it changes nothing the guard reads, or why it does. A name Bash
# neither reads nor sets is free: the guard needs its value only where an expansion of it stands, which leaves its
# word unsettled.
BASH_VARIABLES: dict[str, str] = {
    **dict.fromkeys(
        (
            *("_", "BASH", "BASHPID", "BASH_ARGC", "BASH_ARGV", "BASH_ARGV0", "BASH_COMMAND"),
            *("BASH_EXECUTION_STRING", "BASH_LINENO", "BASH_MONOSECONDS", "BASH_REMATCH", "BASH_SOURCE"),
            *("BASH_SUBSHELL", "BASH_TRAPSIG", "BASH_VERSINFO", "BASH_VERSION", "CHILD_MAX", "COLUMNS"),
            *("COMP_CWORD", "COMP_KEY", "COMP_LINE", "COMP_POINT", "COMP_TYPE", "COMP_WORDBREAKS", "COMP_WORDS"),
            *("COMPREPLY", "COPROC", "COPROC_PID", "DIRSTACK", "EMACS", "EPOCHREALTIME", "EPOCHSECONDS", "EUID"),
            *("FCEDIT", "FIGNORE", "FUNCNAME", "GLOBIGNORE", "GLOBSORT", "GROUPS", "histchars", "HISTCMD"),
            *("HISTCONTROL", "HISTFILE", "HISTFILESIZE", "HISTIGNORE", "HISTSIZE", "HISTTIMEFORMAT", "HOSTFILE"),
            *("HOSTNAME", "HOSTTYPE", "IFS", "IGNOREEOF", "INPUTRC", "INSIDE_EMACS", "LANG", "LC_ALL"),
            *("LC_COLLATE", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME", "LINENO", "LINES", "MACHTYPE"),
            *("MAIL", "MAILCHECK", "MAILPATH", "MAPFILE", "OPTARG", "OPTERR", "OPTIND", "OSTYPE", "PIPESTATUS"),
            *("PPID", "PROMPT_COMMAND", "PROMPT_DIRTRIM", "PS0", "PS1", "PS2", "PS3", "RANDOM", "READLINE_ARGUMENT"),
            *("READLINE_LINE", "READLINE_MARK", "READLINE_POINT", "REPLY", "SECONDS", "SHELL", "SHLVL", "SRANDOM"),
            *("TERM", "TIMEFORMAT", "TMOUT", "TMPDIR", "UID", "auto_resume"),
            # Git Bash's own, from the MSYS runtime.
            *platform_support.GIT_BASH_SHELL_VARIABLES,
        ),
        "",
    ),
    "PATH": "chooses the program every later command name runs",
    "EXECIGNORE": "hides programs from the search, so a later command name runs another",
    "BASH_ALIASES": "its elements are aliases, which a later word runs as other text",
    "BASH_CMDS": "its elements choose the program a later command name runs",
    "BASH_LOADABLES_PATH": "chooses the builtins enable loads",
    "BASH_ENV": "names a file a bash runs as it starts",
    "ENV": "names a file a bash in POSIX mode runs as it starts",
    "HOME": "is where cd with no directory and a leading ~ go, and where git reads its global configuration",
    "PWD": "is where a logical cd .. starts from",
    "OLDPWD": "is where cd - goes",
    "CDPATH": "is searched by cd for a relative directory",
    "PS4": "is expanded before each command xtrace traces, running any substitution it holds",
    "SHELLOPTS": "lists set's options, read-only",
    "BASHOPTS": "lists shopt's options, read-only",
    "POSIXLY_CORRECT": "turns on POSIX mode",
    "BASH_COMPAT": "turns on an earlier Bash's behavior",
    "BASH_XTRACEFD": "sends xtrace's output to a descriptor",
    "FUNCNEST": "limits how deep functions nest",
}

# The kinds of expansion a word may hold, by the form bash_syntax gives each, and the parameter-expansion operators
# inside `${ }`: each understood here leaves its word one the text does not settle, which an argument of a program in
# a program may be, and each other is in NOT_READ.
EXPANSIONS_UNDERSTOOD = {
    "$NAME": "a variable's value",
    "${NAME}": "a variable's value",
    "$@": "the positional parameters",
    **dict.fromkeys(("$*", "$#", "$?", "$-", "$$", "$!", "$0", "$1"), "a special parameter's value"),
    "$(": "a command substitution, whose commands are read",
    "`": "a command substitution, whose commands are read",
    "<(": "a process substitution, whose commands are read",
    ">(": "a process substitution, whose commands are read",
    "~": "the home directory, or another user's or a directory stack's",
    "$'": "an ANSI-C string holding an escape",
    "glob": "pathname expansion, *, ?, and [ ]",
    "brace": "brace expansion, {a,b} and {1..3}",
    **dict.fromkeys(
        (
            *("${NAME:-word}", "${NAME-word}", "${NAME:+word}", "${NAME+word}", "${NAME:?word}", "${NAME?word}"),
            *("${#NAME}", "${NAME#word}", "${NAME##word}", "${NAME%word}", "${NAME%%word}", "${NAME/word}"),
            *("${NAME//word}", "${NAME/#word}", "${NAME/%word}", "${NAME^}", "${NAME^^}", "${NAME,}", "${NAME,,}"),
        ),
        "a variable's value, tested or matched against a word",
    ),
    "${NAME:=word}": "assigns a free name, and is its value",
    "${NAME=word}": "assigns a free name, and is its value",
}
# Bash's parameter-expansion operators after a name inside `${ }`, longest first, by the form they make.
PARAMETER_OPERATORS = (
    (":=", "${NAME:=word}"),
    (":-", "${NAME:-word}"),
    (":+", "${NAME:+word}"),
    (":?", "${NAME:?word}"),
    ("=", "${NAME=word}"),
    ("-", "${NAME-word}"),
    ("+", "${NAME+word}"),
    ("?", "${NAME?word}"),
    ("##", "${NAME##word}"),
    ("#", "${NAME#word}"),
    ("%%", "${NAME%%word}"),
    ("%", "${NAME%word}"),
    ("//", "${NAME//word}"),
    ("/#", "${NAME/#word}"),
    ("/%", "${NAME/%word}"),
    ("/", "${NAME/word}"),
    ("^^", "${NAME^^}"),
    ("^", "${NAME^}"),
    (",,", "${NAME,,}"),
    (",", "${NAME,}"),
    (":", "${NAME:offset}"),
    ("@", "${NAME@op}"),
    ("[", "${NAME[subscript]}"),
)
PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-]")
GLOB = re.compile(r"[*?]|\[.*\]")
BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")

# The PowerShell AST node types (System.Management.Automation.Language, by reflection) the guard understands: a
# list of pipelines of commands, joined by && and ||, whose elements are constants, strings without expansions, and
# parameters, with redirections. Every other type is in NOT_READ.
POWERSHELL_UNDERSTOOD = {
    "ScriptBlockAst": "the command's text, whose statements are read",
    "NamedBlockAst": "the block the statements stand in",
    "PipelineAst": "a pipeline of commands",
    "PipelineChainAst": "pipelines joined by && or ||",
    "CommandAst": "a command and its elements",
    "CommandParameterAst": "a parameter, written as a word",
    "CommandExpressionAst": "an expression standing as a command, read by what it holds",
    "StringConstantExpressionAst": "a literal word or string",
    "ConstantExpressionAst": "a number",
    "ExpandableStringExpressionAst": "a double-quoted string, whose expansions leave its word unsettled",
    "AssignmentStatementAst": "an assignment, understood to a plain variable PowerShell neither sets nor reads itself",
    "MemberExpressionAst": "a property's value, understood for an instance's member a constant names, never a "
    "static one",
    **dict.fromkeys(
        ("ConvertExpressionAst", "TypeExpressionAst", "TypeConstraintAst"),
        "a cast or a type, understood for a type POWERSHELL_CASTS names",
    ),
    "ScriptBlockExpressionAst": "a script block, understood as an argument of a command "
    "POWERSHELL_BLOCK_COMMANDS names, "
    "whose commands are read",
    **dict.fromkeys(
        ("IfStatementAst", "ForEachStatementAst", "StatementBlockAst", "BreakStatementAst", "ContinueStatementAst"),
        "control flow, whose conditions and bodies are read, and a foreach's variable as an assignment",
    ),
    "VariableExpressionAst": "a variable's value, which leaves its word one the text does not settle",
    **dict.fromkeys(
        (
            *("ParenExpressionAst", "SubExpressionAst", "ArrayExpressionAst", "ArrayLiteralAst"),
            *("BinaryExpressionAst", "HashtableAst", "IndexExpressionAst", "TernaryExpressionAst"),
        ),
        "computes a value from what it holds, each command in it read, which leaves its word unsettled",
    ),
    "FileRedirectionAst": "a redirection to a file",
    "MergingRedirectionAst": "a redirection of one stream into another, as 2>&1",
}
# The PowerShell commands the guard understands, by every name and alias PowerShell gives them, lower-cased, each with
# the cmdlet it stands for. A program by any other name is read as in Bash, but for a name PowerShell keeps for its
# own commands (powershell_command), which is not read.
POWERSHELL_LOCATION = {**dict.fromkeys(("set-location", "cd", "chdir", "sl"), "set-location")}
POWERSHELL_PUSH = {**dict.fromkeys(("push-location", "pushd"), "push-location")}
POWERSHELL_POP = {**dict.fromkeys(("pop-location", "popd"), "pop-location")}
POWERSHELL_COMMANDS = {
    **POWERSHELL_LOCATION,
    **POWERSHELL_PUSH,
    **POWERSHELL_POP,
    **dict.fromkeys(("get-childitem", "ls", "dir", "gci"), "get-childitem"),
    **dict.fromkeys(("get-content", "cat", "gc", "type"), "get-content"),
    **dict.fromkeys(("select-string", "sls"), "select-string"),
    **dict.fromkeys(("select-object", "select"), "select-object"),
    **dict.fromkeys(("sort-object", "sort"), "sort-object"),
    **dict.fromkeys(("measure-object", "measure"), "measure-object"),
    **dict.fromkeys(("write-output", "echo", "write"), "write-output"),
    **dict.fromkeys(("get-location", "pwd", "gl"), "get-location"),
    **dict.fromkeys(("get-item", "gi"), "get-item"),
    **dict.fromkeys(("get-command", "gcm"), "get-command"),
    **dict.fromkeys(("get-process", "gps", "ps"), "get-process"),
    **dict.fromkeys(("format-table", "ft"), "format-table"),
    **dict.fromkeys(("foreach-object", "%", "foreach"), "foreach-object"),
    **dict.fromkeys(("where-object", "?", "where"), "where-object"),
    **dict.fromkeys(("group-object", "group"), "group-object"),
    **dict.fromkeys(("format-list", "fl"), "format-list"),
    **dict.fromkeys(("start-sleep", "sleep"), "start-sleep"),
    **dict.fromkeys(("remove-item", "rm", "del", "erase", "rd", "ri", "rmdir"), "remove-item"),
    **dict.fromkeys(("copy-item", "cp", "copy", "cpi"), "copy-item"),
    **dict.fromkeys(("move-item", "mv", "move", "mi"), "move-item"),
    **dict.fromkeys(("new-item", "ni"), "new-item"),
    **dict.fromkeys(("resolve-path", "rvpa"), "resolve-path"),
    **{
        name: name
        for name in (
            *("write-host", "out-string", "out-null", "test-path", "join-path", "split-path", "get-date"),
            *("get-filehash", "convertfrom-json", "convertto-json", "set-content", "add-content", "out-file"),
            "mkdir",
        )
    },
}
# The variables PowerShell sets or reads itself, by Get-Variable in a pwsh started with no profile and by its
# documentation of the automatic and preference variables, lower-cased: an assignment to one, as to
# $PSDefaultParameterValues or $PSNativeCommandArgumentPassing, changes how a later command runs.
POWERSHELL_VARIABLES = frozenset(
    name.lower()
    for name in (
        *("$", "?", "^", "_", "args", "ConfirmPreference", "DebugPreference", "EnabledExperimentalFeatures", "Error"),
        *("ErrorActionPreference", "ErrorView", "Event", "EventArgs", "EventSubscriber", "ExecutionContext", "false"),
        *("foreach", "FormatEnumerationLimit", "HOME", "Host", "InformationPreference", "input", "IsCoreCLR"),
        *("IsLinux", "IsMacOS", "IsWindows", "LASTEXITCODE", "LogCommandHealthEvent", "LogCommandLifecycleEvent"),
        *("LogEngineHealthEvent", "LogEngineLifecycleEvent", "LogProviderHealthEvent", "LogProviderLifecycleEvent"),
        *("Matches", "MaximumHistoryCount", "MyInvocation", "NestedPromptLevel", "null", "OFS", "OutputEncoding"),
        *("PID", "PROFILE", "ProgressPreference", "PSBoundParameters", "PSCmdlet", "PSCommandPath", "PSCulture"),
        *("PSDebugContext", "PSDefaultParameterValues", "PSEdition", "PSEmailServer", "PSHOME", "PSItem"),
        *("PSModuleAutoLoadingPreference", "PSNativeCommandArgumentPassing"),
        *("PSNativeCommandUseErrorActionPreference", "PSScriptRoot", "PSSenderInfo", "PSSessionApplicationName"),
        *("PSSessionConfigurationName", "PSSessionOption", "PSStyle", "PSUICulture", "PSVersionTable", "PWD"),
        *("sender", "ShellId", "StackTrace", "switch", "this", "Transcript", "true", "VerbosePreference"),
        *("WarningPreference", "WhatIfPreference"),
    )
)
# The commands POWERSHELL_COMMANDS names that take a path, on which one on a provider other than the file system is
# not read.
POWERSHELL_PATH_COMMANDS = frozenset(
    {
        *("set-location", "push-location", "get-childitem", "get-content", "get-item", "remove-item", "copy-item"),
        *("move-item", "new-item", "set-content", "add-content", "out-file", "test-path", "resolve-path", "join-path"),
        *("split-path", "get-filehash", "mkdir"),
    }
)
# The types a cast or a type expression may name, lower-cased as PowerShell's accelerators and their full names:
# converting to one runs no code the text does not show.
POWERSHELL_CASTS = frozenset(
    {
        *("string", "int", "int32", "bool", "boolean", "array", "object[]", "hashtable", "pscustomobject"),
        *("psobject", "datetime", "version", "collections.hashtable", "management.automation.pscustomobject"),
    }
)
# The commands whose script-block arguments the guard reads: each runs its block in the session, on its input.
POWERSHELL_BLOCK_COMMANDS = frozenset(
    {"foreach-object", "where-object", "sort-object", "select-object", "group-object"}
)
# The names PowerShell gives a command before it looks for a program, each held to the shell: every alias Get-Alias
# lists in a pwsh started with no profile, each function PowerShell itself defines with no verb in its name (as
# Get-Command lists those of no module), and every verb Get-Verb lists, which begins the name of each cmdlet and
# function. A command by such a name that POWERSHELL_COMMANDS does not hold is not read; any other name is a program.
POWERSHELL_ALIASES = frozenset(
    name.lower()
    for name in (
        *("%", "?", "ac", "cat", "cd", "chdir", "clc", "clear", "clhy", "cli", "clp", "cls", "clv", "cnsn", "compare"),
        *("copy", "cp", "cpi", "cpp", "cvpa", "dbp", "del", "diff", "dir", "dnsn", "ebp", "echo", "epal", "epcsv"),
        *("erase", "etsn", "exsn", "fc", "fhx", "fl", "foreach", "ft", "fw", "gal", "gbp", "gc", "gci", "gcm", "gcs"),
        *("gdr", "gerr", "ghy", "gi", "gin", "gjb", "gl", "gm", "gmo", "gp", "gps", "gpv", "group", "gsn", "gsv"),
        *("gtz", "gu", "gv", "h", "history", "icm", "iex", "ihy", "ii", "ipal", "ipcsv", "ipmo", "irm", "iwr", "kill"),
        *("lp", "ls", "man", "md", "measure", "mi", "mount", "move", "mp", "mv", "nal", "ndr", "ni", "nmo", "nsn"),
        *("nv", "ogv", "oh", "popd", "ps", "pushd", "pwd", "r", "rbp", "rcjb", "rcsn", "rd", "rdr", "ren", "ri"),
        *("rjb", "rm", "rmdir", "rmo", "rni", "rnp", "rp", "rsn", "rv", "rvpa", "sajb", "sal", "saps", "sasv", "sbp"),
        *("scb", "select", "set", "shcm", "si", "sl", "sleep", "sls", "sort", "sp", "spjb", "spps", "spsv", "start"),
        *("stz", "sv", "tee", "type", "where", "wjb", "write"),
    )
)
POWERSHELL_FUNCTIONS = frozenset(
    name.lower()
    for name in (
        *("cd..", "cd\\", "cd~", "help", "mkdir", "more", "oss", "Pause", "prompt", "TabExpansion2", "Configuration"),
        *("ImportSystemModules", "PSConsoleHostReadLine"),
        # pwsh off Windows: replaces the session with the program its words name, a runner the guard does not read.
        "exec",
        *(f"{letter}:" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    )
)
# Of them, those that write or remove what their path names, which on the environment provider sets or removes a
# variable git reads.
POWERSHELL_WRITING_COMMANDS = frozenset(
    {"set-content", "add-content", "out-file", "new-item", "copy-item", "move-item", "remove-item", "mkdir"}
)
# Of them, the parameters each binds a value given by position to, in order; a value past them is read as a path. The
# one Copy-Item and Move-Item copy or move from, and the one each other writes, is "path", whichever name binds it.
POWERSHELL_POSITIONS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("set-content", "add-content"), ("path", "value")),
    "out-file": ("path", "encoding"),
    **dict.fromkeys(("copy-item", "move-item"), ("path", "destination")),
    **dict.fromkeys(("new-item", "mkdir", "remove-item"), ("path",)),
}
# Their own parameters, each with its aliases, held to Get-Command, and the common parameters every cmdlet takes:
# PowerShell binds a parameter written as its name or an alias, or as a prefix that names one of the cmdlet's own
# parameters alone, or, where it names none of them, one common parameter alone (powershell_parameter).
POWERSHELL_COMMON_PARAMETERS: dict[str, tuple[str, ...]] = {
    "verbose": ("vb",),
    "debug": ("db",),
    "erroraction": ("ea",),
    "warningaction": ("wa",),
    "informationaction": ("infa",),
    "progressaction": ("proga",),
    "errorvariable": ("ev",),
    "warningvariable": ("wv",),
    "informationvariable": ("iv",),
    "outvariable": ("ov",),
    "outbuffer": ("ob",),
    "pipelinevariable": ("pv",),
    "whatif": ("wi",),
    "confirm": ("cf",),
}
_CONTENT_PARAMETERS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("value", "passthru", "path", "filter", "include", "exclude", "force", "credential"), ()),
    # Stream, the file-system provider's on Windows, names an alternate data stream of the path.
    **dict.fromkeys(("nonewline", "encoding", "asbytestream", "stream"), ()),
    "literalpath": ("pspath", "lp"),
}
_ITEM_PARAMETERS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("path", "filter", "include", "exclude", "force", "credential"), ()),
    "literalpath": ("pspath", "lp"),
}
POWERSHELL_CMDLET_PARAMETERS: dict[str, dict[str, tuple[str, ...]]] = {
    "set-content": _CONTENT_PARAMETERS,
    "add-content": _CONTENT_PARAMETERS,
    "copy-item": {
        **_ITEM_PARAMETERS,
        **dict.fromkeys(("destination", "container", "recurse", "passthru", "fromsession", "tosession"), ()),
    },
    "move-item": {**_ITEM_PARAMETERS, **dict.fromkeys(("destination", "passthru"), ())},
    "remove-item": {**_ITEM_PARAMETERS, "recurse": (), "stream": ()},
    "new-item": {
        **dict.fromkeys(("path", "name", "force", "credential"), ()),
        "itemtype": ("type",),
        "value": ("target",),
    },
    "mkdir": dict.fromkeys(("path", "name", "value", "force", "credential"), ()),
    "out-file": {
        "filepath": ("path",),
        "literalpath": ("pspath", "lp"),
        **dict.fromkeys(("encoding", "append", "force", "width", "nonewline", "inputobject"), ()),
        "noclobber": ("nooverwrite",),
    },
}
# Of those, the parameters whose value is a path they write, read, or create; New-Item's -Name is joined to each path
# it is given. The ones that bind "path" are POWERSHELL_PATH_BINDING, the literal ones, whose value is no wildcard
# pattern, POWERSHELL_LITERAL_PATHS.
POWERSHELL_PATH_PARAMETERS = frozenset({"path", "literalpath", "filepath", "destination", "name"})
POWERSHELL_PATH_BINDING = frozenset({"path", "literalpath", "filepath"})
POWERSHELL_LITERAL_PATHS = frozenset({"literalpath"})
# Their parameters that take a value which is no path, and those that take none; a parameter neither names is read
# as taking no value, so the value after it is read as one given by position, a path.
POWERSHELL_DATA_PARAMETERS = frozenset(
    {
        *("value", "encoding", "itemtype", "filter", "include", "exclude", "credential", "stream", "width"),
        *("inputobject", "tosession", "fromsession", "erroraction", "warningaction", "informationaction"),
        *("errorvariable", "warningvariable", "informationvariable", "outvariable", "outbuffer", "pipelinevariable"),
        "progressaction",
    }
)
# The common parameters that take a value, which is no path.
POWERSHELL_COMMON_DATA = frozenset(
    name for name in POWERSHELL_COMMON_PARAMETERS if name not in {"verbose", "debug", "whatif", "confirm"}
)
POWERSHELL_SWITCHES = frozenset(
    {
        *("force", "recurse", "passthru", "nonewline", "append", "noclobber", "whatif", "confirm", "asbytestream"),
        *("container", "verbose", "debug", "usetransaction"),
    }
)
# The item types New-Item makes that are no link, each of which its file-system provider takes by any prefix of its
# name; any other, as SymbolicLink, Junction, or HardLink, makes a link (LINK_PROGRAMS).
POWERSHELL_ITEM_TYPES = ("file", "directory")
# The characters a PowerShell path given to -Path, or by position, matches as a wildcard pattern, which pwsh off Windows
# also expands in a native program's word.
POWERSHELL_WILDCARDS = re.compile(r"[*?\[]")
# A PowerShell path on a provider other than the file system: a drive whose name is more than one letter, such as
# env:, alias:, or HKLM:, or a provider-qualified path such as Environment::.
PROVIDER_PATH = re.compile(r"^(?:(?:[\w.]+\\)?[A-Za-z]\w*::|[A-Za-z][\w-]+:)")

# Every catalogue entry the guard does not read, with why: a command holding one is refused while a guarded hub is in
# view. Keyed by its catalogue and its name as that catalogue lists it.
NOT_READ = {
    # Bash builtins.
    **{f"builtin {name}": "runs a file's commands, which the guard does not read" for name in (".", "source")},
    "builtin alias": "defines a name a later word runs as other text",
    "builtin unalias": "changes what a later word runs",
    **{f"builtin {name}": "job control, which a command run as a script does not use" for name in ("bg", "fg")},
    "builtin disown": "job control, which a command run as a script does not use",
    "builtin suspend": "job control, which a command run as a script does not use",
    "builtin logout": "a login shell's exit, which a command run as a script does not use",
    "builtin bind": "binds a key to a command, which -x runs",
    "builtin builtin": "runs the builtin its next word names",
    "builtin enable": "loads a builtin or turns one off, which changes what a later name runs",
    "builtin eval": "runs text as commands",
    "builtin exec": "replaces the shell with the command its words name, or changes its file descriptors",
    "builtin fc": "runs commands again from the history",
    "builtin hash": "makes a name run another program, with -p",
    "builtin history": "edits the history, from which fc runs commands",
    "builtin trap": "runs text as commands when a signal or a command fires it",
    "builtin let": "evaluates arithmetic, which may assign any variable and run a subscript's substitution",
    "builtin getopts": "assigns variables from the positional parameters",
    "builtin unset": "removes a variable or a function, by a name the guard does not follow",
    "builtin dirs": "prints or clears the directory stack popd reads",
    **{
        f"builtin {name}": "declares a variable: exports it, makes a name reference or an integer of it, which change "
        "what a later command sees or evaluates"
        for name in ("declare", "typeset", "local", "export", "readonly")
    },
    **{f"builtin {name}": "assigns an array, and runs a callback with -C" for name in ("mapfile", "readarray")},
    **{
        f"builtin {name}": "completion, whose -C and -F run a command or a function"
        for name in ("compgen", "complete", "compopt")
    },
    # Bash reserved words, and the constructs the grammar reads without one.
    "keyword [[": "a conditional expression, whose arithmetic comparisons and -v evaluate arithmetic",
    "keyword ]]": "ends a conditional expression",
    "keyword function": "defines a function, which may take any command name",
    "keyword coproc": "runs a command as a coprocess, in the background with its descriptors kept",
    "construct function definition": "defines a function, which may take any command name, git's among them",
    "construct arithmetic command": "(( )) evaluates arithmetic, which may assign any variable",
    "construct arithmetic for": "for (( )) evaluates arithmetic, which may assign any variable",
    "construct element assignment": "NAME[SUBSCRIPT]= evaluates its subscript as arithmetic",
    "construct named descriptor": "a redirection's {NAME} assigns the descriptor it opens to NAME, any variable",
    "construct assignment before git": "an assignment before git may change where it acts or what configuration "
    "it reads",
    # set's and shopt's options, in the state the tables refuse.
    "set -o allexport": "exports each later assignment, so a name reaches git's environment",
    "set -o keyword": "puts each assignment word after a command's name into its environment, as GIT_DIR=<dir> after "
    "git",
    "set -o posix": "changes how assignments before special builtins, aliases, and expansions are read",
    "set -o physical": "cd resolves symbolic links, where the guard reads a directory as Bash's logical cd does",
    "set -o interactive-comments": "off, a word starting with # is no comment",
    "shopt cdable_vars": "a cd to a name that is no directory takes the variable by that name as one",
    "shopt lastpipe": "the last command of a pipeline runs in the shell itself, so a cd there moves it",
    "shopt cdspell": "cd corrects a misspelled directory to another",
    **{f"shopt {name}": "an earlier Bash's behavior" for name in ("compat31", "compat32", "compat40", "compat41")},
    **{f"shopt {name}": "an earlier Bash's behavior" for name in ("compat42", "compat43", "compat44")},
    "shopt extdebug": "runs the debugger's profile, and lets a DEBUG trap skip or replace commands",
    "shopt interactive_comments": "off, a word starting with # is no comment",
    # Expansions.
    "expansion $((": "arithmetic, which may assign any variable and run a subscript's substitution",
    "expansion $[": "arithmetic, which may assign any variable and run a subscript's substitution",
    "expansion NAME=( )": "an array assignment, which evaluates each element's subscript as arithmetic",
    "expansion ${NAME[subscript]}": "a subscript, evaluated as arithmetic",
    "expansion ${NAME:offset}": "an offset and length, evaluated as arithmetic",
    "expansion ${!NAME}": "indirection, which evaluates a subscript the value names, or lists names",
    "expansion ${NAME@op}": "a transformation, whose @P expands a prompt and runs what it holds",
    "expansion ${ command; }": "runs a command in the shell itself, Bash 5.3's",
    "expansion ${| command; }": "runs a command in the shell itself and takes REPLY, Bash 5.3's",
    "expansion nested ${": "an expansion inside another's word, which the guard does not read apart",
    "expansion ${unread}": "a braced expansion of a form the guard does not read",
    "expansion assigning": "assigns a variable Bash or git reads, or one the environment holds",
    # PowerShell AST node types.
    **{
        f"PowerShell {name}": "computes a value the text does not settle"
        for name in ("AttributedExpressionAst", "UsingExpressionAst")
    },
    "PowerShell UnaryExpressionAst": "++ and -- assign the variable they hold, which may be the environment's",
    **{
        f"PowerShell {name}": "reaches a member, which may run any code, git among it"
        for name in ("InvokeMemberExpressionAst", "BaseCtorInvokeMemberExpressionAst")
    },
    **{
        f"PowerShell {name}": "defines a function, a type, a configuration, or a parameter, or loads a module"
        for name in (
            *("FunctionDefinitionAst", "TypeDefinitionAst", "FunctionMemberAst", "PropertyMemberAst"),
            *("ConfigurationDefinitionAst", "DynamicKeywordStatementAst", "UsingStatementAst", "ParamBlockAst"),
            *("ParameterAst", "AttributeAst", "NamedAttributeArgumentAst"),
        )
    },
    **{
        f"PowerShell {name}": "a statement the guard does not read in PowerShell"
        for name in (
            *("SwitchStatementAst", "ForStatementAst", "WhileStatementAst", "DoWhileStatementAst"),
            *("DoUntilStatementAst", "TryStatementAst", "CatchClauseAst", "TrapStatementAst", "DataStatementAst"),
            *("BlockStatementAst", "ReturnStatementAst", "ExitStatementAst", "ThrowStatementAst"),
        )
    },
    **{
        f"PowerShell {name}": "text PowerShell could not parse, which runs nothing"
        for name in ("ErrorStatementAst", "ErrorExpressionAst")
    },
}

GIT_NAMES = {"git"}
# How deep a substitution and a shell alias are followed; deeper is a command the guard does not read.
NESTING_LIMIT = 4
POWERSHELL_READER = Path(__file__).with_name("hub_guard-powershell.ps1")
# How long the guard reads and judges one command, pwsh included, before it is a command the guard cannot read in
# time. The hook's own 30-second timeout in .claude/settings.json blocks a command it cuts short (onFailure), so this
# leaves room for starting Python and writing the decision; the hub lookups before and after reading, and each git
# call they start, are held to it too (lookups_within).
READING_SECONDS = 15
# How much of the reader's output is kept: far more than any command a tool call carries, and a reading cut short is
# not JSON, so it is a command the guard cannot read rather than one read in part.
POWERSHELL_OUTPUT_LIMIT = 1 << 22
# How many paths a command names are looked up for a guarded hub when none is in view and it holds a part the guard
# does not read.
NAMED_PATH_LIMIT = 32
DENY_REASON = (
    "This checkout is the hub: it stays on main and is never edited, so parallel sessions cannot carry each "
    "other's changes (docs/parallel-sessions.md). Create a worktree with "
    "`python tools/worktrees.py new <kind> <name>`, enter it, and work there."
)
PLACE_IT = "Write the directory into the command itself, as `git -C <dir>` or `cd <dir> &&`, and run it again."
INHERITED_IT = (
    "The session inherited the variable, which no command can unset here: restart the session from an environment "
    "without {name}, or run git from a terminal."
)
INDEX_IT = INHERITED_IT.format(name="GIT_INDEX_FILE")
CONFIGURED_TRACE_IT = (
    "No command can change the git configuration here: remove {name} from it from a terminal, or restart the session "
    "from an environment that sets {variable} to 0, which outranks it."
)
CONFIGURED_WRITE_IT = (
    "No command can change the git configuration here: remove {name} from it from a terminal, then run the command "
    "again."
)
UNREAD_CONFIGURATION_IT = "Repair the git configuration from a terminal, then run the command again."
# The refusal the guard gives when its own reason holds what JSON cannot carry, such as a lone surrogate.
UNWRITABLE_REASON = f"The guard refused this command for a reason that holds what JSON cannot carry. {DENY_REASON}"
REWRITE_IT = (
    "Write it as plain commands with literal words, running each program by its name rather than through another "
    "that runs it (RUNNERS in tools/hub_guard.py), and run it again; the guard's contract in "
    "docs/parallel-sessions.md lists what it reads."
)


# The strings that spell gh's --limit, by the scope that holds them, and why each is no gh listing.
GH_LIMIT_ALLOWED = {
    "GIT_OPERAND_WRITERS": "holds git merge-file's -L, the label option, which takes a value; no gh call reads it",
}


# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "powershell_events": "starts the pwsh platform_support.find_pwsh located on the session's PATH",
}


GIT_STATUS_ALLOWED = {
    "guard_enabled": (
        "reports git's reason to the user: exit 1 is git's answer that the opt-in is unset, and any other failure "
        "skips the guard with a note naming git's stderr, as the guard's contract says"
    ),
    "git_builtins": "fails closed: an empty list makes every subcommand a possible alias, which is read and judged",
    "shadowed_fast_forward": "fails closed: any failure to list the refs refuses the fast-forward",
    "Judge.alias_refusal": (
        "fails closed: any failure but exit 1 refuses the call; exit 1 is git's answer that no such alias is set, or "
        "that the name is one no alias can have, which git cannot run either"
    ),
}


def same_place(left: Path | None, right: Path | None) -> bool:
    return left is right or (left is not None and right is not None and same_path(left, right))


def guard_enabled(hub: Path) -> bool:
    """Reads the opt-in from the repository's own configuration file only: --local passes over command-line
    configuration, which GIT_CONFIG_PARAMETERS and GIT_CONFIG_COUNT carry and which would otherwise outrank it. git
    exits 1 for a setting that is not there; any other failure, such as a value that is no boolean, is a hub it cannot
    look up, allowed with a note rather than read as unguarded unseen."""
    result = git(hub, "config", "--local", "--type=bool", "--get", GUARD_SETTING)
    if result.returncode not in (0, 1):
        said = flat_text(result.stderr) or f"git config exited with {result.returncode}"
        print(f"hub guard skipped: the opt-in of {hub} could not be read: {said}", file=sys.stderr)
    return result.returncode == 0 and output_line(result.stdout) == "true"


@functools.cache
def guarded_hub_at(path: Path) -> Path | None:
    """The guarded hub git acts on from path: the one whose checkout holds it, or, inside a git directory, where git
    has no work tree and acts on that directory's repository, the one whose own git directory it is; None when it is
    a worktree, another repository, or none."""
    checkout = checkout_of(path)
    if checkout is None:
        repository = git_directory_holding(path)
        return None if repository is None else guarded_hub_of_repository(repository)
    if not checkout.is_hub or not guard_enabled(checkout.hub):
        return None
    return checkout.hub


def git_directory_holding(path: Path) -> Path | None:
    """The git directory git finds from path when path lies inside one; None in a work tree, outside every
    repository, and where nothing of path exists.

    Raises GitError when git answers otherwise with an error, or with lines other than the two asked for, so a start
    git cannot read is never read as one outside every repository."""
    directory = worktrees.existing_directory(path)
    if directory is None:
        return None
    arguments = ["rev-parse", "--path-format=absolute", "--is-inside-git-dir", "--git-dir"]
    try:
        output = CLIENT.output(arguments, directory=directory, timeout=lookup_timeout())
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    lines = output_lines(output)
    if len(lines) != 2:
        raise GitError(f"git rev-parse answered {len(lines)} lines for the git directory holding {path}, not two")
    return Path(lines[1]) if lines[0] == "true" else None


@functools.cache
def guarded_hub_of_repository(path: Path) -> Path | None:
    """The guarded hub whose own git directory path names, as --git-dir or GIT_DIR does; None for a worktree's, another
    repository's, or a path git answers names no repository, where git runs nothing.

    Raises GitError when git cannot tell, or the path or the folder above it cannot be looked at, so each caller
    refuses rather than read the path as no guarded hub's.
    """
    directory = path if worktrees.is_folder(path) else path.parent
    if not worktrees.is_folder(directory):
        return None
    arguments = ["--git-dir", str(path), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    try:
        output = CLIENT.output(arguments, directory=directory, timeout=lookup_timeout())
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    lines = output_lines(output)
    if len(lines) != 2:
        raise GitError(f"git rev-parse answered {len(lines)} lines for the git directory {path}, not two")
    if not same_path(Path(lines[0]), Path(lines[1])):
        return None
    common = Path(lines[1])
    if platform_support.name_key(common.name, common.parent) != ".git":
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
    return frozenset(output_lines(result.stdout)) if result.returncode == 0 else frozenset()


def hub_protected(path: Path) -> bool:
    """Whether path lies in the tracked surface of a guarded hub or anywhere in its git directory (protecting_hub), as
    written or with every link on the way to it followed, since a write through a link reaches what the link names.

    A checkout or git directory git answers with an error for may be a guarded hub's, so it is protected; where git
    did not run or finish, the GitError goes to the hook, which allows the edit with a note."""
    try:
        if protecting_hub(path) is not None:
            return True
        real = Path(os.path.realpath(path))
        return real != Path(os.path.normpath(path)) and protecting_hub(real) is not None
    except GitError as exc:
        if not exc.answered:
            raise
        return True


def protecting_hub(path: Path) -> Path | None:
    """The guarded hub whose git directory holds path, anywhere in it, or whose checkout holds it among the files it
    does not ignore; ignored files such as local settings are no hub's. None where no guarded hub holds it.

    Raises GitError where git cannot tell, as guarded_hub_of_repository and guarded_hub_at do, and where check-ignore
    answers neither, which hub_protected reads as a protected path when git answered."""
    normalized = Path(os.path.normpath(path))
    for directory in (normalized, *normalized.parents):
        if platform_support.name_key(directory.name, directory.parent) == ".git" and worktrees.is_folder(directory):
            return guarded_hub_of_repository(directory)
    hub = guarded_hub_at(normalized)
    if hub is None:
        return None
    # check-ignore exits 0 for an ignored path and 1, saying nothing, for one that is not; any other answer raises.
    arguments = ["check-ignore", "-q", "--no-index", str(normalized)]
    ignored = CLIENT.quiet_output(arguments, directory=hub, timeout=lookup_timeout()) is not None
    return None if ignored else hub


def hubs_in_view(cwd: Path, environment: Mapping[str, str]) -> set[Path]:
    """The guarded hubs a session can reach: the hub of its own directory's checkout, a worktree's included, and of
    its project's, or the guarded hub whose git directory holds one. A directory whose checkout git cannot read is
    noted and passed over, so the others still count."""
    directories = [cwd]
    project = environment.get("CLAUDE_PROJECT_DIR")
    if project:
        directories.append(Path(platform_support.from_shell_path(project)))
    found: set[Path] = set()
    for directory in directories:
        try:
            checkout = checkout_of(directory)
            hub = guarded_hub_at(directory) if checkout is None else checkout.hub
        except GitError as exc:
            print(f"hub guard could not look up the hub of {directory}: {exc}", file=sys.stderr)
            continue
        if hub is not None and guard_enabled(hub):
            found.add(hub)
    return found


# ---------------------------------------------------------------------------------------------------------------
# What the readers produce


@dataclass(frozen=True)
class Place:
    """Where one git call acts, as far as the text settles it.

    start is where git starts, after each -C; tree is the work tree whose files it writes; repository is the git
    directory whose HEAD and index it writes when one is named, and otherwise git finds it from start. None is a
    place the text does not settle. aliases holds the alias.<name> settings given on the command line, or is None when
    command-line configuration the text does not settle may hold any. index is the index file GIT_INDEX_FILE names, as
    written, when index_named, None where the text does not settle it; index_reached reads it.
    """

    start: Path | None
    tree: Path | None
    repository: Path | None = None
    repository_named: bool = False
    aliases: Mapping[str, str | None] | None = field(default_factory=dict)
    index_named: bool = False
    index: Word = None


@dataclass(frozen=True)
class GitCall:
    place: Place
    subcommand: str | None  # None when the text does not settle it
    arguments: list[str | None]  # None for a word the text does not settle
    # git's own options as read before the subcommand, from which an alias's expansion goes on reading.
    options: GitOptions | None = None
    # gh for a gh command that runs git itself to check a branch out.
    program: str = "git"

    @property
    def directory(self) -> Path | None:
        return self.place.tree


@dataclass(frozen=True)
class Write:
    """A file a command writes at a path its text names, and what a refusal says writes it, as `The command redirects
    its output into`; for a pattern, path is the folder every file it matches lies in, and for a folder, as one a
    download writes into, path is that folder."""

    path: Path
    opening: str
    pattern: bool = False
    folder: bool = False


@dataclass(frozen=True)
class ScriptRun:
    """A Python script a command runs, at the path its words place, with what the guard reads from its words as from
    any program read_unlisted reads: the files they may name and the parts it does not read. Both count only where the
    script is not one TRUSTED_SCRIPTS names that a guarded hub tracks (is_trusted_script)."""

    script: Path
    writes: list[Write]
    unread: list[str]


@dataclass
class Reading:
    """What the guard read from one command: the git calls it makes, in order, the files its redirections, programs,
    and cmdlets write, the parts of it the guard does not read, which are refused while a guarded hub is in view, the
    paths it names, and notes on what it passed over."""

    environment: Mapping[str, str]
    text: str = ""
    calls: list[GitCall] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    # Commands refused while a hub is guarded with a remedy of their own, which no worktree gives (OwnRemedy).
    refusals: list[str] = field(default_factory=list)
    scripts: list[ScriptRun] = field(default_factory=list)
    named: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    context_hubs: set[Path] = field(default_factory=set)
    # The session's own directory and project, where git may read the configuration otherwise than the guard does.
    places: list[Path] = field(default_factory=list)
    # When the guard's reading time ends, by reading_clock.
    deadline: float = math.inf

    def cannot_read(self, what: str) -> None:
        """A part of the command the guard does not read: refused while a guarded hub is in view."""
        if what not in self.unread:
            self.unread.append(what)

    def refuse(self, reason: str) -> None:
        """A command refused while a guarded hub is in view, with a remedy of its own, wherever it runs."""
        if reason not in self.refusals:
            self.refusals.append(reason)

    def time_left(self) -> float:
        """The seconds left of the reading time; past it, raises ReadingTimeout, which the hook refuses as a failure
        of the guard's own when the command names git while a guarded hub is in view."""
        return time_left(self.deadline)


class OwnRemedy(str):
    """A refusal that names its own remedy, which the hook gives without DENY_REASON: the command is refused from a
    worktree too, so creating one is no remedy."""


class ReadingTimeout(Exception):
    """The guard's reading time ended before the command was read and judged."""


class LookupsCutShort(Exception):
    """The command names more directories than NAMED_PATH_LIMIT, so some were never looked up for a guarded hub."""


def reading_clock() -> float:
    return time.monotonic()


def time_left(deadline: float) -> float:
    """The seconds until deadline, by reading_clock; past it, raises ReadingTimeout."""
    left = deadline - reading_clock()
    if left <= 0:
        raise ReadingTimeout(f"the guard did not finish reading it within {READING_SECONDS} seconds")
    return left


@dataclass
class LookupBound:
    """When the hub lookups must end, by reading_clock: the reading deadline of the command being read and judged,
    while it is, and no bound otherwise, as for a file event, whose git calls each keep GUARD_GIT_TIMEOUT_SECONDS."""

    deadline: float = math.inf


LOOKUPS = LookupBound()


def process_seconds(seconds: float) -> float:
    """The wall-clock limit handed to a process the guard starts, git or pwsh: seconds, the part of the reading time
    or of GUARD_GIT_TIMEOUT_SECONDS it may take. Every such limit passes through here, as every look at the time
    passes through reading_clock, so a test that holds the clock still also gives each process a limit of its own
    rather than race a real start against what is left of the reading time."""
    return seconds


def lookup_timeout() -> float:
    """How long one git call of a hub lookup may take: GUARD_GIT_TIMEOUT_SECONDS, or the reading time left when that
    is less; past the reading time, raises ReadingTimeout, so a lookup starts no git call the deadline has passed."""
    return process_seconds(min(time_left(LOOKUPS.deadline), GUARD_GIT_TIMEOUT_SECONDS))


@contextlib.contextmanager
def lookups_within(deadline: float) -> Iterator[None]:
    """Holds every hub lookup made inside the block, and each git call it starts, to deadline."""
    outer, LOOKUPS.deadline = LOOKUPS.deadline, deadline
    try:
        yield
    finally:
        LOOKUPS.deadline = outer


def git(cwd: Path, *arguments: str) -> GitResult:
    """A git call the guard makes, run as tools/worktrees.py runs one, within what lookup_timeout leaves it; the two
    lookups of a git directory, git_directory_holding and guarded_hub_of_repository, call CLIENT.output themselves,
    within the same limit."""
    return worktrees.git(cwd, *arguments, timeout=lookup_timeout())


def checkout_of(path: Path) -> Checkout | None:
    """The checkout holding path, as tools/worktrees.py finds it, its git call within what lookup_timeout leaves it."""
    return worktrees.checkout_of(path, lookup_timeout())


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
    """The lower-cased name a command is called by, without its directory, after either separator on every platform, or
    .exe; None when it is computed. The host's own path type would leave a Windows path whole on Linux, where a guard
    reading a command meant for Windows must still name its program."""
    if word is None:
        return None
    return platform_support.program_name(word.replace("\\", "/").rpartition("/")[2]).lower()


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
    return {name: value for name, value in environment.items() if is_tracked(name)}


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
        # git reads the count as an int, so six digits is generous, and a longer one would keep the loop below going
        # for practically ever; a count past it is one the guard cannot settle.
        entries = None if count is None else whole_number.parse(count.strip(), limit=6)
        if entries is None:
            configuration.settled = False
            return configuration
        for index in range(entries):
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
    # The index file GIT_INDEX_FILE names, which no option of git's own does.
    index_file: tuple[bool, Word] = (False, None)

    @classmethod
    def initial(cls, cwd: Path | None, values: Mapping[str, Word], opaque: bool) -> GitOptions:
        if opaque:
            # Variables the text does not show may name any repository, work tree, or index file.
            return cls(cwd, (True, None), (True, None), Configuration(settled=False), (True, None))
        return cls(
            cwd,
            ("GIT_DIR" in values, values.get("GIT_DIR")),
            ("GIT_WORK_TREE" in values, values.get("GIT_WORK_TREE")),
            environment_configuration(values),
            ("GIT_INDEX_FILE" in values, values.get("GIT_INDEX_FILE")),
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
        named_index, index = self.index_file
        return Place(self.start, tree, repository, named_dir, self.configuration.aliases(), named_index, index)


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
            read_setting(value, reading)
        elif option == "--config-env":
            # The value comes from a variable the text names, which the guard does not follow; the key it settles.
            if value is None:
                options.configuration.settled = False
            else:
                options.configuration.entries.append((value.partition("=")[0].lower(), None))
            read_setting(value, reading)
        elif word == "--bare":
            options.git_dir = options.git_dir if options.git_dir[0] else (True, ".")
        elif option not in GIT_OPTIONS_WITH_VALUE and word not in GIT_OPTIONS_WITHOUT_VALUE:
            reading.notes.append(f"git's option {word} is not one the guard reads")
            return -1
        index += 2 if separate else 1
    return None


def read_setting(setting: Word, reading: Reading) -> None:
    """A setting -c or --config-env hands git: understood when GIT_SETTINGS_UNDERSTOOD names its key or
    GIT_SECTIONS_UNDERSTOOD its section, in any case, and otherwise a part the guard does not read, since it may run a
    command or choose where git writes."""
    if setting is None:
        reading.cannot_read("git is handed a setting the text does not settle, which may run a command")
        return
    key = setting.partition("=")[0].lower()
    if key in GIT_SETTINGS_UNDERSTOOD or key.partition(".")[0] in GIT_SECTIONS_UNDERSTOOD:
        return
    reading.cannot_read(f"git is handed {key}, not a setting the guard knows to be safe; one may run a command")


def read_git(arguments: list[Word], cwd: Path | None, reading: Reading) -> None:
    """One git call: its place, from the variables the hook's environment gives it and its own options, and its
    subcommand. The command itself assigns none it is read with: an assignment before git, and every builtin that
    exports one, is a part the guard does not read."""
    values = git_variables(inherited_variables(reading.environment), reading.notes)
    record_git_call(arguments, GitOptions.initial(cwd, values, opaque=False), reading)


def record_git_call(arguments: list[Word], options: GitOptions, reading: Reading) -> None:
    index = read_git_options(arguments, options, reading)
    if index is None:
        return
    if index < 0 or arguments[index] is None:
        if index >= 0:
            reading.notes.append("a git subcommand is computed at run time")
        reading.calls.append(GitCall(options.place(), None, [], options.copy()))
        return
    # A word the text does not settle is kept as None, which refusal reads as any option.
    reading.calls.append(GitCall(options.place(), arguments[index], arguments[index + 1 :], options.copy()))


# ---------------------------------------------------------------------------------------------------------------
# Judging a call


def options(text: str) -> frozenset[str]:
    """The option spellings a whitespace-separated text names, so a table row reads as git's own help does."""
    return frozenset(text.split())


Operands = Callable[[Sequence[Word], frozenset[str]], str | None]


@dataclass(frozen=True)
class ReadOnlyForm:
    """One form of a git subcommand a guarded hub allows, read by read_only_refusal.

    Every option word is spelled in full, as one of these, or refused: flags take no value; valued take one, attached
    after = (a long option) or as the rest of the word (a letter), or as the next word, a call whose next word starts
    with a dash being refused, since git takes it as the value; attached take one only attached, and may stand alone.
    A bundle of letters is read only when every letter is a flag. required holds the options of which at least one must
    be given, as --ff-only for a fast-forward; counts allows a count such as -5; separator allows a `--`, after which
    every word is an operand; and operands judges the operands, given the options used, None allowing any. judge,
    where set, decides the call alone, as git config's reading forms do. values judges the value of the options it
    names, as the branch `worktree add -b` creates must be a task's (task_branch). listed says `git <form> -h` lists
    every option the row declares outside REVISION_OPTIONS and DIFF_OPTIONS, with the kind it declares, which
    tests/tools/test_hub_guard_git_options.py holds.
    """

    reason: str
    flags: frozenset[str] = frozenset()
    valued: frozenset[str] = frozenset()
    attached: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    counts: bool = False
    separator: bool = True
    operands: Operands | None = None
    judge: Callable[[Sequence[Word]], str | None] | None = None
    listed: bool = True
    values: Mapping[str, Callable[[str], str | None]] = field(default_factory=dict)


def exactly(*expected: str) -> Operands:
    """Operands that must be these words and no others."""

    def judge(operands: Sequence[Word], given: frozenset[str]) -> str | None:
        if list(operands) == list(expected):
            return None
        shown = " ".join("<unsettled>" if word is None else word for word in operands) or "no operand"
        return f"it takes only {' '.join(expected)}, not {shown}"

    return judge


def fast_forward(subcommand: str, *target: str) -> Operands:
    """The one fast-forward the hub takes: to origin/main, named in full, never to an upstream a setting chooses."""

    def judge(operands: Sequence[Word], given: frozenset[str]) -> str | None:
        if list(operands) == list(target):
            return None
        shown = " ".join("<unsettled>" if word is None else word for word in operands) or "its upstream"
        return f"`git {subcommand} --ff-only` moves the hub only to origin/main, not to {shown}."

    return judge


def no_operands(operands: Sequence[Word], given: frozenset[str]) -> str | None:
    return None if not operands else f"it takes no operand, and {operands[0]} would be one"


def fetched_refs(operands: Sequence[Word], given: frozenset[str]) -> str | None:
    """A fetch's repository and refspecs: none may name a ref to write, as `x:refs/remotes/origin/main` does, which a
    later fast-forward to origin/main would follow."""
    for word in operands:
        if word is None or ":" in word:
            return "a refspec with a destination, or one the text does not settle, writes a ref a fast-forward follows"
    return None


def listed_with(*listing: str) -> Operands:
    """Operands a listing reads as patterns only with one of its listing options; without one, they create a ref."""

    def judge(operands: Sequence[Word], given: frozenset[str]) -> str | None:
        if not operands or given & set(listing):
            return None
        return f"without {' or '.join(listing)} its operands create a ref"

    return judge


def branch_operands(operands: Sequence[Word], given: frozenset[str]) -> str | None:
    """git branch deletes the branches it names with -d, -D, or --delete, and otherwise lists them only with -l or
    --list; any other operand creates a branch."""
    if given & {"-d", "-D", "--delete"}:
        return None if operands else "it deletes no branch"
    return listed_with("-l", "--list")(operands, given)


def task_branch(name: str) -> str | None:
    """A branch a hub form creates must be a task's, `<kind>/<name>` as tools/worktrees.py names one, so it never
    shadows a ref the hub follows, as `origin/main` would shadow refs/remotes/origin/main."""
    kind, slash, rest = name.partition("/")
    if slash and kind in worktrees.KINDS and worktrees.SLUG.fullmatch(rest):
        return None
    return f"a branch the hub creates is a task's, <kind>/<name> as tools/worktrees.py names one, not {name or 'none'}"


def config_form(arguments: Sequence[Word]) -> str | None:
    """git config in one of its reading forms (config_reads); any other form may write a setting."""
    if config_reads(arguments)[0]:
        return None
    return "it may write a setting, which the configuration a hub shares with its worktrees holds"


# The revision options of log and the commands that read revisions as it does, none of which writes or runs a command;
# --output, --ext-diff, --textconv, and the options that run a program are not among them. -n is log's own and is
# added where a row takes it, since shortlog spells --numbered with it.
REVISION_OPTIONS = ReadOnlyForm(
    reason="",
    flags=options(
        "--oneline --graph --all --branches --tags --remotes --first-parent --merges --no-merges --reverse "
        "--topo-order --date-order --author-date-order --abbrev-commit --no-abbrev-commit --no-decorate --source "
        "--left-right --cherry-pick --cherry-mark --cherry --left-only --right-only --boundary --ancestry-path "
        "--full-history --simplify-by-decoration --follow --parents --children --relative-date -i "
        "--regexp-ignore-case -E --extended-regexp -F --fixed-strings --basic-regexp --all-match --invert-grep "
        "--no-notes --mailmap --no-mailmap --use-mailmap --dense --sparse --count --show-pulls"
    ),
    valued=options("--max-count --skip --since --after --until --before --author --committer --grep -L --exclude"),
    attached=options("--pretty --format --decorate --date --abbrev --glob --no-walk"),
)
# The diff options of diff, show, and log that change only what they print.
DIFF_OPTIONS = ReadOnlyForm(
    reason="",
    flags=options(
        "-p --patch -u --no-patch -s --raw --numstat --shortstat --summary --name-only --name-status --full-index "
        "--binary --no-color --no-renames --minimal --patience --histogram -w --ignore-all-space -b "
        "--ignore-space-change --ignore-blank-lines --ignore-space-at-eol --ignore-cr-at-eol -R --text -a "
        "--no-ext-diff --no-textconv --exit-code --quiet --check --cached --staged --no-prefix --cc -m "
        "--compact-summary --patch-with-stat --irreversible-delete -D --find-copies-harder --merge-base -z"
    ),
    valued=options("-S -G --diff-filter --diff-algorithm --src-prefix --dst-prefix --anchored"),
    attached=options(
        "--stat --dirstat --color --color-moved --word-diff --color-words --find-renames -M --find-copies -C "
        "--unified -U --abbrev --diff-merges --relative --submodule --ignore-submodules --inter-hunk-context"
    ),
)


def read_only(reason: str, *groups: ReadOnlyForm, **row: Any) -> ReadOnlyForm:
    """A row reading revisions or diffs: the options of each group, beside the row's own."""
    merged = {
        "flags": frozenset().union(row.get("flags", frozenset()), *(group.flags for group in groups)),
        "valued": frozenset().union(row.get("valued", frozenset()), *(group.valued for group in groups)),
        "attached": frozenset().union(row.get("attached", frozenset()), *(group.attached for group in groups)),
    }
    return ReadOnlyForm(reason, **{**row, **merged})


LOG_FORM = {"valued": options("-n"), "counts": True}
# The git forms a guarded hub allows: its subcommand, or its subcommand and the word of its own subcommand, as
# `worktree add`, with the options each may take and what it does. Every other subcommand, option, abbreviation, and
# form is refused in a hub, since git's whole surface is too wide to list what writes ("The guard's contract" in
# docs/parallel-sessions.md). Each row reads, or keeps the hub on main and current: pulling main, creating and removing
# the worktrees and branches of tasks, and surveying. A row may not name an option git's own help says writes or runs a
# command, nor one git does not list for it (tests/tools/test_hub_guard_git_options.py), and every git command the
# hub's documents and repository skills run is one of them (tests/tools/test_hub_guard.py).
READ_ONLY_FORMS: dict[str, ReadOnlyForm] = {
    "status": ReadOnlyForm(
        "shows the work tree's state",
        flags=options(
            "-v --verbose -s --short -b --branch --show-stash --ahead-behind --no-ahead-behind --long -z --null "
            "--no-renames --renames --no-column"
        ),
        attached=options("--porcelain -u --untracked-files --ignored --ignore-submodules --column -M --find-renames"),
    ),
    "log": read_only("shows history", REVISION_OPTIONS, DIFF_OPTIONS, **LOG_FORM, listed=False),
    "show": read_only("shows objects", REVISION_OPTIONS, DIFF_OPTIONS, **LOG_FORM, listed=False),
    "diff": read_only("shows changes", DIFF_OPTIONS, listed=False),
    "rev-list": read_only("lists commits", REVISION_OPTIONS, **LOG_FORM, flags=options("--objects"), listed=False),
    "shortlog": read_only(
        "summarizes history",
        REVISION_OPTIONS,
        flags=options("-c --committer -n --numbered -s --summary -e --email"),
        valued=options("--group"),
        attached=options("-w"),
    ),
    "reflog": read_only("shows a reflog", REVISION_OPTIONS, DIFF_OPTIONS, **LOG_FORM, listed=False),
    "reflog show": read_only("shows a reflog", REVISION_OPTIONS, DIFF_OPTIONS, **LOG_FORM, listed=False),
    "stash list": read_only("lists stashes", REVISION_OPTIONS, DIFF_OPTIONS, **LOG_FORM, listed=False),
    "stash show": read_only("shows a stash", DIFF_OPTIONS, listed=False),
    "rev-parse": ReadOnlyForm(
        "names objects and paths",
        flags=options(
            "--show-toplevel --git-dir --git-common-dir --absolute-git-dir --verify -q --quiet --symbolic "
            "--symbolic-full-name --is-inside-work-tree --is-inside-git-dir --is-bare-repository "
            "--is-shallow-repository --show-prefix --show-cdup --show-superproject-working-tree --local-env-vars "
            "--all --branches --tags --remotes --revs-only --no-revs --flags --no-flags --sq --not"
        ),
        valued=options("--git-path --default"),
        attached=options("--short --abbrev-ref --path-format --show-object-format"),
        listed=False,
    ),
    "ls-files": ReadOnlyForm(
        "lists the index's files",
        flags=options(
            "-z -t -v -f -c --cached -d --deleted -m --modified -o --others -i --ignored -s --stage -k --killed "
            "--directory --eol --no-empty-directory -u --unmerged --resolve-undo --exclude-standard --full-name "
            "--recurse-submodules --error-unmatch --debug --deduplicate --sparse"
        ),
        valued=options("-x --exclude -X --exclude-from --exclude-per-directory --with-tree --format"),
        attached=options("--abbrev"),
    ),
    "ls-tree": ReadOnlyForm(
        "lists a tree",
        flags=options("-d -r -t -z -l --long --name-only --name-status --object-only --full-name --full-tree"),
        valued=options("--format"),
        attached=options("--abbrev"),
    ),
    "cat-file": ReadOnlyForm(
        "prints objects",
        flags=options(
            "-e -p -t -s --use-mailmap --mailmap -Z --batch-all-objects --buffer --follow-symlinks --unordered"
        ),
        attached=options("--batch --batch-check --batch-command"),
    ),
    "show-ref": ReadOnlyForm(
        "lists refs",
        flags=options("--tags --branches --exists --verify --head -d --dereference -q --quiet"),
        attached=options("-s --hash --abbrev --exclude-existing"),
    ),
    "for-each-ref": ReadOnlyForm(
        "lists refs",
        flags=options("-s --shell -p --perl --python --tcl --omit-empty --ignore-case --include-root-refs"),
        valued=options("--count --format --sort --points-at --merged --no-merged --contains --no-contains --exclude"),
        attached=options("--color"),
    ),
    "merge-base": ReadOnlyForm(
        "finds common ancestors", flags=options("-a --all --octopus --independent --is-ancestor --fork-point")
    ),
    "describe": ReadOnlyForm(
        "names a commit by its tags",
        flags=options("--contains --all --tags --long --first-parent --exact-match --always"),
        valued=options("--candidates --match --exclude"),
        attached=options("--abbrev --dirty --broken"),
    ),
    "grep": ReadOnlyForm(
        "searches files",
        flags=options(
            "--cached --untracked --exclude-standard --no-exclude-standard --recurse-submodules -v --invert-match -i "
            "--ignore-case -w --word-regexp -a --text -I -r --recursive -E --extended-regexp -G --basic-regexp -F "
            "--fixed-strings -P --perl-regexp -n --line-number --column -h -H --full-name -l --files-with-matches "
            "--name-only -L --files-without-match -z --null -o --only-matching -c --count --no-color --break "
            "--heading -p --show-function -W --function-context --and --or --not -q --quiet --all-match"
        ),
        valued=options(
            "--max-depth -C --context -B --before-context -A --after-context --threads -f -e -m --max-count"
        ),
        attached=options("--color"),
        counts=True,
    ),
    "blame": ReadOnlyForm(
        "shows who changed each line",
        flags=options(
            "-l -t -s -e -f -n -p -w -b --porcelain --line-porcelain --root --show-email --show-name --show-number "
            "--incremental --show-stats"
        ),
        valued=options("-L --ignore-rev"),
    ),
    "check-attr": ReadOnlyForm(
        "shows attributes", flags=options("-a --all --cached --stdin -z"), valued=options("--source")
    ),
    "check-ignore": ReadOnlyForm(
        "shows which paths are ignored",
        flags=options("-q --quiet -v --verbose --stdin -z -n --non-matching --no-index"),
    ),
    "ls-remote": ReadOnlyForm(
        "lists a remote's refs",
        flags=options("-q --quiet -t --tags -b --branches --refs --get-url --exit-code --symref"),
        valued=options("--sort -o --server-option"),
    ),
    "count-objects": ReadOnlyForm(
        "counts objects", flags=options("-v --verbose -H --human-readable"), operands=no_operands
    ),
    "cherry": ReadOnlyForm(
        "lists commits not yet upstream", flags=options("-v --verbose"), attached=options("--abbrev")
    ),
    "version": ReadOnlyForm("prints git's version", operands=no_operands, listed=False),
    "config": ReadOnlyForm("reads a setting", judge=config_form, listed=False),
    "remote": ReadOnlyForm("lists the remotes", flags=options("-v --verbose"), separator=False, operands=no_operands),
    "remote get-url": ReadOnlyForm("prints a remote's address", flags=options("--push --all"), separator=False),
    "remote show": ReadOnlyForm("describes a remote", flags=options("-n"), separator=False),
    "worktree list": ReadOnlyForm(
        "lists the worktrees",
        flags=options("--porcelain -v --verbose -z"),
        valued=options("--expire"),
        separator=False,
        operands=no_operands,
    ),
    "worktree add": ReadOnlyForm(
        "creates a task's worktree on a new task branch, never a second checkout of main, which -f would allow, nor a "
        "branch git names itself or one that shadows a ref the hub follows",
        flags=options("-q --quiet"),
        valued=options("-b"),
        required=options("-b"),
        separator=False,
        values={"-b": task_branch},
    ),
    "worktree remove": ReadOnlyForm(
        "removes a task's worktree, never one with changes, which -f would allow", separator=False
    ),
    "worktree prune": ReadOnlyForm(
        "forgets worktrees whose folders are gone",
        flags=options("-n --dry-run -v --verbose"),
        valued=options("--expire"),
        separator=False,
        operands=no_operands,
    ),
    "branch": ReadOnlyForm(
        "lists branches, and deletes any local branch git will delete, not only a task's, or with -r any "
        "remote-tracking ref; git refuses to delete a branch a worktree has checked out; never creates, renames, "
        "copies, or forces one, nor sets an upstream",
        flags=options(
            "-v --verbose -q --quiet -r --remotes -a --all -d --delete -D -l --list --show-current -i --ignore-case "
            "--no-color --omit-empty"
        ),
        valued=options("--contains --no-contains --merged --no-merged --sort --points-at --format"),
        attached=options("--color --abbrev --column"),
        separator=False,
        operands=branch_operands,
    ),
    "tag": ReadOnlyForm(
        "lists tags, never creating or deleting one",
        flags=options("-l --list -i --ignore-case --omit-empty"),
        valued=options("--contains --no-contains --merged --no-merged --sort --points-at --format"),
        attached=options("-n --column --color"),
        separator=False,
        operands=listed_with("-l", "--list"),
    ),
    "fetch": ReadOnlyForm(
        "updates the remote-tracking refs, never the branch HEAD names (-u) nor a ref a refspec names, nor an upstream",
        flags=options(
            "-v --verbose -q --quiet --all -t --tags -n --no-tags -p --prune --dry-run --porcelain --progress "
            "--no-progress --no-recurse-submodules -4 --ipv4 -6 --ipv6 --show-forced-updates "
            "--no-show-forced-updates"
        ),
        valued=options("-j --jobs -o --server-option"),
        operands=fetched_refs,
    ),
    "pull": ReadOnlyForm(
        "fast-forwards main to origin's main, named in full rather than an upstream a setting chooses",
        flags=options("--ff-only -q --quiet -v --verbose --no-rebase"),
        required=options("--ff-only"),
        separator=False,
        operands=fast_forward("pull", "origin", "main"),
    ),
    "merge": ReadOnlyForm(
        "fast-forwards main to origin/main",
        flags=options("--ff-only -q --quiet -v --verbose"),
        required=options("--ff-only"),
        separator=False,
        operands=fast_forward("merge", "origin/main"),
    ),
    "switch": ReadOnlyForm(
        "returns the hub to main", flags=options("-q --quiet"), separator=False, operands=exactly(MAIN)
    ),
    "checkout": ReadOnlyForm(
        "returns the hub to main", flags=options("-q --quiet"), separator=False, operands=exactly(MAIN)
    ),
    "apply": ReadOnlyForm(
        "checks a patch or shows its statistics, never applying it, which only --apply or a later --no-check does",
        flags=options(
            "--check --stat --numstat --summary -v --verbose -q --quiet -R --reverse -z --recount --inaccurate-eof "
            "--allow-empty --unidiff-zero --ignore-space-change --ignore-whitespace"
        ),
        valued=options("--exclude --include -p -C --whitespace"),
        required=options("--check --stat --numstat --summary"),
    ),
}
# The subcommands a row of READ_ONLY_FORMS names: none is an alias, since each is a builtin, which no alias can be.
READ_ONLY_SUBCOMMANDS = frozenset(key.split(" ")[0] for key in READ_ONLY_FORMS)


def read_only_form(subcommand: str, arguments: Sequence[Word]) -> tuple[str, ReadOnlyForm | None, Sequence[Word]]:
    """The row of READ_ONLY_FORMS a call is read by, its key, and the words after the key: a subcommand some row names
    with its own subcommand's word takes its first word as that word when it is no option, and a row of the
    subcommand alone is read only when the first word is an option or there is none, so `reflog delete` is no
    `reflog`."""
    first = arguments[0] if arguments else None
    if first is not None and not first.startswith("-") and f"{subcommand} {first}" in READ_ONLY_FORMS:
        key = f"{subcommand} {first}"
        return key, READ_ONLY_FORMS[key], arguments[1:]
    actions = any(key.startswith(f"{subcommand} ") for key in READ_ONLY_FORMS)
    if actions and first is not None and not first.startswith("-"):
        return f"{subcommand} {first}", None, arguments[1:]
    return subcommand, READ_ONLY_FORMS.get(subcommand), arguments


# Why a word the text does not settle, before any `--`, is refused in a hub: it may be any option.
UNSETTLED_WORD = "is given a word the text does not settle, which may be any of its options."


def read_only_refusal(subcommand: str, arguments: Sequence[Word]) -> str | None:
    """Why a git call in a guarded hub is refused: unless READ_ONLY_FORMS declares its form and every option it uses,
    spelled in full (read_only_words), and its operands are ones the row allows; None when it is one of them."""
    key, form, words = read_only_form(subcommand, arguments)
    refused = f"`git {key}` is not a read-only form the hub allows"
    if form is None:
        return f"{refused}; the hub allows only the forms READ_ONLY_FORMS in tools/hub_guard.py declares."
    if form.judge is not None:
        why = form.judge(words)
        return None if why is None else f"{refused}: {why}."
    read = read_only_words(form, words)
    if read == UNSETTLED_WORD:
        return f"`git {key}` {read}"
    if isinstance(read, str):
        return f"{refused} {read}"
    given, operands = read
    if form.required and not given & form.required:
        return f"{refused} without {' or '.join(sorted(form.required))}."
    why = form.operands(operands, given) if form.operands is not None else None
    if why is None:
        return None
    return why if why.startswith("`git") else f"{refused}: {why}."


def read_only_words(form: ReadOnlyForm, words: Sequence[Word]) -> tuple[frozenset[str], list[Word]] | str:
    """The options a call gives and its operands, read by a READ_ONLY_FORMS row; or, for the first word the row does
    not declare, the end of the reason it is refused."""
    given: set[str] = set()
    operands: list[Word] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word is None:
            return UNSETTLED_WORD
        if word == "--":
            if not form.separator:
                return "with `--`, after which its operands are paths."
            operands.extend(words[index:])
            break
        if word == "-" or not word.startswith("-"):
            operands.append(word)
            continue
        spelled = read_option(form, word)
        if spelled is None:
            return f"with {word}: its row declares no such option, spelled so."
        given.update(spelled)
        # A next word the text does not settle is never taken as a value: the next pass refuses it. git takes the next
        # word as a lone valued option's value whatever it holds, so one that starts with a dash is refused rather than
        # read as an option of its own, which git would not read.
        following = words[index] if index < len(words) else ""
        if following is not None and spelled[-1] == word and word in form.valued:
            if following.startswith("-"):
                return dash_value_refusal(word, following)
            index += 1
        why = value_refusal(form, word, spelled[-1], following or "")
        if why is not None:
            return why
    return frozenset(given), operands


def dash_value_refusal(option: str, following: str) -> str:
    """Why a valued option spelled alone is refused before a word that starts with a dash: git takes that word as its
    value, so the guard does not read it as an option; the value is to be attached instead."""
    attached = f"{option}=<value>" if option.startswith("--") else f"{option}<value>"
    return f"with {option} {following}: git takes {following} as the value of {option}; attach it, as {attached}."


def value_refusal(form: ReadOnlyForm, word: str, option: str, following: str) -> str | None:
    """Why the value a row judges (ReadOnlyForm.values) is refused: the value attached to the word, after = or as the
    rest of a letter's word, or else the word after it, which git takes whatever it holds."""
    judge = form.values.get(option)
    if judge is None:
        return None
    if word.startswith("--"):
        value = word.partition("=")[2] if "=" in word else following
    else:
        value = word[len(option) :] or following
    why = judge(value)
    return None if why is None else f"with {option} {value}: {why}."


def read_option(form: ReadOnlyForm, word: str) -> list[str] | None:
    """The options of a READ_ONLY_FORMS row one word spells in full, None where it spells none: a long option exactly
    or with its value after =; a count where the row takes one; a letter alone or with its value attached; or a bundle
    of letters each a flag, which spells each of them. A valued option spelled alone is the word itself, so its value
    is the next word."""
    if word.startswith("--"):
        name, attached, _ = word.partition("=")
        declared = form.valued | form.attached if attached else form.flags | form.valued | form.attached
        return [name] if name in declared else None
    if form.counts and word[1:].isdigit():
        return [word]
    letter = word[:2]
    if letter in form.valued | form.attached:
        return [letter]
    letters = [f"-{each}" for each in word[1:]]
    return letters if all(each in form.flags for each in letters) else None


# The refs git reads `origin/main` as before refs/remotes/origin/main, or as ambiguous beside it (gitrevisions): the
# fast-forward to origin/main is refused while any of them exists, since git would follow it instead.
FAST_FORWARD_SHADOWS = (
    "refs/origin/main",
    "refs/tags/origin/main",
    "refs/heads/origin/main",
    "refs/remotes/origin/main/HEAD",
)


def shadowed_fast_forward(hub: Path) -> str | None:
    """Why `merge --ff-only origin/main` in a hub is refused: a ref of that short name other than
    refs/remotes/origin/main exists, which git resolves first or reads as ambiguous, or git cannot list them."""
    result = git(hub, "for-each-ref", "--format=%(refname)", *FAST_FORWARD_SHADOWS)
    if result.returncode != 0:
        said = flat_text(result.stderr) or f"git for-each-ref exited with {result.returncode}"
        return (
            f"`git merge --ff-only origin/main` is refused: the guard could not list the refs it may follow ({said})."
        )
    shadows = output_lines(result.stdout)
    if not shadows:
        return None
    return (
        f"`git merge --ff-only origin/main` would follow {shadows[0]}, not refs/remotes/origin/main; delete that ref "
        "from a terminal, or pull with `git pull --ff-only origin main`."
    )


def hub_refusal(call: GitCall) -> str | None:
    """Why a call that acts on a guarded hub is refused, or None: gh checking a branch out, and every git form
    READ_ONLY_FORMS does not declare."""
    if call.program == "gh":
        return f"`gh {call.subcommand}` checks a branch out in the checkout it runs in."
    if call.subcommand is None:
        return "The guard could not tell which git subcommand runs."
    return read_only_refusal(call.subcommand, call.arguments)


@dataclass
class GitEffects:
    """What a git call's words make it do beyond its subcommand: the paths it writes, as written, None for one the text
    does not settle; the option or word that makes it run a command; and whether a word the text does not settle may
    be an option that does either."""

    written: list[Word] = field(default_factory=list)
    runs: str | None = None
    unsettled: bool = False


def option_spelled(word: str, options: Iterable[str]) -> tuple[str, str | None] | None:
    """The option of options a word spells, in every spelling git's option parser reads, and its value when the word
    carries it: a long option exactly or abbreviated to a prefix of its name, its value after =; a short one alone,
    with its value attached, or in a bundle, where its letter ends the bundle and the rest is its value. None when the
    word spells none of them. A value of "" says the option takes the next word."""
    if word.startswith("--"):
        name, attached, value = word.partition("=")
        if len(name) > 2:
            for option in sorted(options):
                if option.startswith("--") and option.startswith(name):
                    return option, value if attached else ""
        return None
    if word.startswith("-") and len(word) > 1:
        for position, letter in enumerate(word[1:], start=1):
            if f"-{letter}" in options:
                return f"-{letter}", word[position + 1 :]
    return None


def running_spelling(subcommand: str, arguments: Sequence[Word]) -> str | None:
    """The word of a git call that makes it start a command or a program its words name: one spelling an option of
    GIT_RUNNING_OPTIONS, read by git_client's carried_option, which reads every word wherever it stands, and a bisect
    or submodule action outside git_client's PLAIN_ACTIONS; None where there is none."""
    settled = [word for word in arguments if word is not None]
    if subcommand in GIT_RUNNING_OPTIONS:
        carried = carried_option(settled, GIT_RUNNING_OPTIONS[subcommand])
        if carried is not None:
            return carried
    action = next((word for word in settled if not word.startswith("-")), None)
    if subcommand in PLAIN_ACTIONS and action is not None and action not in PLAIN_ACTIONS[subcommand]:
        return action
    return None


def git_effects(subcommand: str, arguments: Sequence[Word]) -> GitEffects:
    """The files a git call writes at paths its words name, by GIT_OUTPUT, GIT_WRITING_OPTIONS, GIT_DEFAULT_WRITES,
    GIT_OPERAND_WRITERS, and GIT_UNREAD_MODES, and the command it runs (running_spelling). An option's value is the
    next word when the word carries none; words after `--` are operands. A path the text does not settle is None."""
    writing = {GIT_OUTPUT, *GIT_WRITING_OPTIONS.get(subcommand, ())}
    effects = GitEffects(runs=running_spelling(subcommand, arguments))
    named = False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if word is None:
            effects.unsettled = True
            continue
        if word == "--":
            break
        spelled = option_spelled(word, writing)
        if spelled is None:
            continue
        value = spelled[1]
        if value == "" and index < len(arguments):
            value = arguments[index]
            index += 1
        if value != "":
            effects.written.append(value)
            named = True
    unread = GIT_UNREAD_MODES.get(subcommand)
    if unread is not None:
        valued = unread.valued | GIT_WRITING_OPTIONS.get(subcommand, frozenset())
        if mode_given(option_words(arguments, valued, unread.modes)[0], valued, unread.modes, False):
            # The paths it writes are the ones a file it reads names.
            effects.written.append(None)
    if subcommand not in GIT_OPERAND_SUBCOMMANDS and stdout_given(arguments):
        # format-patch prints its patches; a writer's own row names the options with which it prints.
        return effects
    found = operand_writer(subcommand, arguments)
    if found is not None:
        written = operand_writes(found[0], found[1], frozenset(writing))
        effects.written.extend(written)
        named = bool(written) if subcommand in {"clone", "init", "init-db"} else named
    if subcommand in GIT_DEFAULT_WRITES and not named:
        effects.written.append(".")
    return effects


def operand_writer(subcommand: str, arguments: Sequence[Word]) -> tuple[OperandWriter, Sequence[Word]] | None:
    """The row of GIT_OPERAND_WRITERS a git call is read by, and the words after its own subcommand's word where the
    row's key names one; None for a call no row names."""
    if subcommand in GIT_OPERAND_WRITERS:
        return GIT_OPERAND_WRITERS[subcommand], arguments
    first = arguments[0] if arguments else None
    key = f"{subcommand} {first}"
    if first is not None and key in GIT_OPERAND_WRITERS:
        return GIT_OPERAND_WRITERS[key], arguments[1:]
    return None


def operand_writes(writer: OperandWriter, arguments: Sequence[Word], writing: frozenset[str]) -> list[Word]:
    """The operands a call a GIT_OPERAND_WRITERS row names writes: none where the last of its modes given is off, or
    one of its stdout options is given; otherwise the ones its first and count name, of the words that are no option
    and no option's value, after `--` every word. A word the text does not settle is kept as an operand, so the
    result may hold None."""
    valued = writer.valued | writing
    options, operands = option_words(arguments, valued, writer.modes | writer.stdout)
    if not mode_given(options, valued, writer.modes, not writer.modes) or mode_given(
        options, valued, writer.stdout, False
    ):
        return []
    end = None if writer.count is None else writer.first + writer.count
    return operands[writer.first : end]


def stdout_given(arguments: Sequence[Word]) -> bool:
    """Whether a call that writes in the directory git starts in is told to print instead, by the last --stdout or
    --no-stdout before `--`, read fail-safe: format-patch takes git's revision and diff options, many of which take
    the next word as their value and no table lists, so --stdout counts only where the word before it can be no
    option awaiting one: none, a word that is no option, a count such as -1, an option whose value follows =, or
    --stdout or --no-stdout itself. A --no-stdout always counts, since it only makes the call write."""
    on = False
    previous: Word = ""
    for word in arguments:
        if word == "--":
            break
        if word is not None and word.startswith("--"):
            if option_spelled(word, {"--stdout"}) is not None:
                settled = previous is not None and (
                    not previous.startswith("-")
                    or previous[1:].isdigit()
                    or "=" in previous
                    or option_spelled(previous, {"--stdout", "--no-stdout"}) is not None
                )
                on = on or settled
            elif option_spelled(word, {"--no-stdout"}) is not None:
                on = False
        previous = word
    return on


def option_words(
    arguments: Sequence[Word], valued: frozenset[str], flags: frozenset[str]
) -> tuple[list[str], list[Word]]:
    """A call's option words before `--`, each option's value skipped (takes_next_word), and its operands: every other
    word, after `--` every word, a word the text does not settle kept as an operand. An option's value spelled as an
    option, as `--exclude --no-unsafe-paths`, is a value and so is neither."""
    operands: list[Word] = []
    options: list[str] = []
    words = iter(arguments)
    for word in words:
        if word is None or word == "-" or not word.startswith("-"):
            operands.append(word)
            continue
        if word == "--":
            operands.extend(words)
            break
        options.append(word)
        if takes_next_word(word, valued, flags):
            next(words, None)
    return options, operands


def takes_next_word(word: str, valued: frozenset[str], flags: frozenset[str]) -> bool:
    """Whether an option word takes the next word as its value: a long option of valued spelled with no `=`, or a
    bundle of letters whose first one valued names ends it; a letter flags names before it stands alone."""
    if word.startswith("--"):
        if word.startswith("--no-") and option_spelled(f"--{word[5:]}", flags | valued) is not None:
            return False
        spelled = option_spelled(word, valued)
        return spelled is not None and spelled[1] == ""
    for position, letter in enumerate(word[1:], start=1):
        if f"-{letter}" in valued:
            return position == len(word) - 1
    return False


def mode_given(options: Sequence[str], valued: frozenset[str], modes: frozenset[str], default: bool) -> bool:
    """Whether the last of a call's option words that turns one of modes on or off leaves it on; default where none
    does. A long option is read exactly or abbreviated, `--no-<name>` turning it off; a letter is read in a bundle up to
    the first letter valued names, whose value is the rest."""
    on = default
    for word in options:
        if not word.startswith("-") or word == "-":
            continue
        if word.startswith("--"):
            if option_spelled(word, modes) is not None:
                on = True
            elif word.startswith("--no-") and option_spelled(f"--{word[5:]}", modes) is not None:
                on = False
            continue
        for letter in word[1:]:
            if f"-{letter}" in modes:
                on = True
            if f"-{letter}" in valued:
                break
    return on


def config_reads(arguments: Sequence[Word]) -> tuple[bool, Word | None]:
    """Whether a git config only reads: with an action that reads (CONFIG_READ_OPTIONS), a subcommand that does
    (CONFIG_READ_ACTIONS), or one name and no action, and otherwise only CONFIG_MODIFIERS and CONFIG_VALUED; and the
    file --file or -f names, when one does and no scope does. A word the text does not settle and an option those
    tables do not hold, an abbreviation among them, make it one that may write."""
    names: list[str] = []
    action = False
    file: Word | None = None
    scoped = False
    words = iter(arguments)
    for word in words:
        if word is None:
            return False, None
        if word == "--":
            names.extend("" if name is None else name for name in words)
            break
        if not word.startswith("-") or word == "-":
            names.append(word)
            continue
        if word in CONFIG_READ_OPTIONS:
            action = True
            continue
        if word in CONFIG_MODIFIERS:
            scoped = scoped or word in CONFIG_SCOPES
            continue
        option, attached, value = word.partition("=")
        if word.startswith("-f") and len(word) > 2 and not word.startswith("--"):
            option, attached, value = "-f", "=", word[2:]
        if option not in CONFIG_VALUED:
            return False, None
        given = value if attached else next(words, None)
        if option in {"-f", "--file"}:
            file = given
    # A key holds a dot, so one name without one, as `git config edit`, is a subcommand.
    reads = action or (bool(names) and names[0] in CONFIG_READ_ACTIONS) or (len(names) == 1 and "." in names[0])
    return reads, None if scoped else file


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
            try:
                hub = hub or guarded_hub_of_repository(place.repository)
            except GitError:
                unknown = True  # a repository git cannot read may be a guarded hub
    elif place.start is None:
        unknown = True
    else:
        hub = hub or guarded_hub_at(place.start)
    return hub, unknown


def index_reached(place: Place) -> tuple[Path | None, bool]:
    """The guarded hub that holds the index file GIT_INDEX_FILE names for a call at place, unless that index lies in
    the call's own worktree's git directory, which git writes for the call anyway, and whether the text does not
    settle it.

    One form is accepted: an absolute path to an index no guarded hub holds (protecting_hub), or to any file in the
    per-worktree git directory of the linked worktree a call's repository is (own_paths), its own `index` and the lock
    files git writes beside it, as `index.lock` and `next-index-<pid>.lock`, among them, since a hook or an editor git
    starts mid-operation hands one on. Any other a guarded hub holds is that hub's: its own index, even from the hub,
    since a call such as `worktree add` or `worktree remove` starts a git that inherits the variable in another
    checkout, whose `reset --hard` or clean check then writes the hub's index out of step with its HEAD; another
    worktree's; and any file in its git directory or among the files of its checkout it does not ignore. git follows a
    symbolic link to write the index through its lock file, so each link is followed here too. A relative path is not
    settled: git reads it from whatever directory each process it starts works in, a child such as `worktree add`'s
    `reset --hard` in another one, and git exports a relative index only for a repository whose git directory is
    relative, which no worktree's is.
    """
    if not place.index_named:
        return None, False
    if place.index is None:
        return None, True
    named = Path(platform_support.from_shell_path(place.index))
    if not named.is_absolute():
        return None, True
    index = Path(os.path.realpath(named))
    try:
        hub = protecting_hub(index)
        if hub is None:
            return None, False
        own = own_paths(place)
        if own is not None and own.linked and within(index, own.git_dir):
            return None, False
        return hub, False
    except GitError:
        return None, True  # an index git cannot place may be a guarded hub's


def within(path: Path, folder: Path) -> bool:
    """Whether path is folder or lies anywhere beneath it, each compared as platform_support compares a path."""
    return any(same_path(each, folder) for each in (path, *path.parents))


@dataclass(frozen=True)
class OwnPaths:
    """Where git keeps the repository of a call when no variable names another place, as `git rev-parse` answers it:
    the per-worktree git directory, the common directory every worktree of it shares, and the object store."""

    git_dir: Path
    common_dir: Path
    objects: Path

    @property
    def linked(self) -> bool:
        """Whether the git directory is a linked worktree's own, apart from the common directory, which a main
        checkout's, a hub's among them, is."""
        return not same_path(self.git_dir, self.common_dir)


def own_paths(place: Place) -> OwnPaths | None:
    """Where git keeps the repository of a call at place (OwnPaths), the guard's own git being given none of git's
    repository-selecting variables; None at a place where git finds no repository, and where the text does not settle
    where git starts or the repository it is named. Each path is read with every symbolic link followed."""
    directory = place.start or place.tree
    if directory is None or (place.repository_named and place.repository is None) or not worktrees.is_folder(directory):
        return None
    repository = ["--git-dir", str(place.repository)] if place.repository_named and place.repository else []
    asked = ["--git-dir", "--git-common-dir", "--git-path", "objects"]
    try:
        output = CLIENT.output(
            [*repository, "rev-parse", "--path-format=absolute", *asked], directory=directory, timeout=lookup_timeout()
        )
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    lines = output_lines(output)
    if len(lines) != 3:
        raise GitError(f"git rev-parse answered {len(lines)} lines for the repository of {directory}, not three")
    git_dir, common_dir, objects = (Path(os.path.realpath(line)) for line in lines)
    return OwnPaths(git_dir, common_dir, objects)


def store_reached(place: Place, name: str, value: str) -> tuple[Path | None, bool, bool]:
    """For the store an inherited STORE_VARIABLES variable names for a call at place: the guarded hub that holds it
    (protecting_hub), unless it is the call's own (own_paths), which git writes anyway; whether the guard cannot place
    it, as a relative path, which git reads from wherever it works, or one git cannot answer for; and whether it is the
    call's own. Each symbolic link is followed, as git follows one."""
    given = Path(platform_support.from_shell_path(value))
    if not given.is_absolute():
        return None, True, False
    store = Path(os.path.realpath(given))
    try:
        own = own_paths(place)
        if own is not None and same_path(store, own.objects if name == "GIT_OBJECT_DIRECTORY" else own.common_dir):
            return None, False, True
        return protecting_hub(store), False, False
    except GitError:
        return None, True, False  # a store git cannot place may be a guarded hub's


def call_named(call: GitCall) -> str:
    """A call as a refusal names it."""
    return f"`git {call.subcommand or '<computed>'}`" if call.program == "git" else f"`gh {call.subcommand}`"


def git_writes_trace_at(value: str) -> bool:
    """Whether git writes a trace at the path a GIT_TRACE variable's value, or a trace2 target setting's, names: one git
    reads as absolute, starting with a separator or a drive, as git for Windows also reads one (is_absolute_path in
    git's sources)."""
    return value.startswith(("/", "\\")) or bool(re.match(r"[A-Za-z]:", value))


@dataclass(frozen=True)
class WrittenTarget:
    """A file the session's environment has every git write a trace or its output to: what names it, as a note says
    so, the sentence a refusal opens with, how to take it out, and the file with each symbolic link followed, None
    where the guard cannot place it."""

    source: str
    named: str
    remedy: str
    path: Path | None
    # Why a target the guard cannot place may lie in a guarded hub, as a refusal says it.
    unplaced: str = "a file git writes that the guard cannot place, which may lie in a guarded hub"
    # What writes there, as a refusal names it.
    writer: str = "every git a command starts, a program's own included,"


# What writes a file the session's environment or configuration names beyond git's trace and output files.
TLS_WRITER = "every https call a command makes, a git call's or another program's,"
COOKIE_WRITER = "every https git call a command starts, a program's own included,"
SOCKET_WRITER = "git's file-system monitor, which a git call a command starts may start,"


def placed_file(value: str) -> Path | None:
    """The absolute file a value names, with every symbolic link followed; None for a path this platform does not read
    as absolute."""
    path = Path(platform_support.from_shell_path(value))
    return Path(os.path.realpath(path)) if path.is_absolute() else None


def written_targets(variables: Mapping[str, str]) -> list[WrittenTarget]:
    """The files the inherited environment names for git to write its trace or its output to, by the name git reads
    (folded): the absolute path a GIT_TRACE variable names (TRACE_VARIABLE), and every value of a REDIRECT_VARIABLES
    one but those that name no file; and the file KEY_LOG_VARIABLE names for libcurl, which git's https transport runs,
    to write its TLS keys to. A path the guard cannot place is one this platform does not read as absolute, and a
    relative one git for Windows or libcurl reads from where it starts."""
    targets: list[WrittenTarget] = []
    for name, value in sorted(variables.items()):
        if value in DEVICE_FILES:
            continue
        if name == KEY_LOG_VARIABLE:
            if value:
                named = f"The session's environment holds {name}={value}"
                remedy = INHERITED_IT.format(name=name)
                unplaced = "a relative path each program opens from where it starts, which may lie in a guarded hub"
                targets.append(WrittenTarget(name, named, remedy, placed_file(value), unplaced, TLS_WRITER))
            continue
        if TRACE_VARIABLE.fullmatch(name):
            if not git_writes_trace_at(value):
                continue
        elif name not in REDIRECT_VARIABLES or value in REDIRECT_VARIABLES[name]:
            continue
        named = f"The session's environment holds {name}={value}"
        targets.append(WrittenTarget(name, named, INHERITED_IT.format(name=name), placed_file(value)))
    return targets


def configured_written_targets(
    variables: Mapping[str, str], start: Path | None, places: Iterable[Path] = ()
) -> list[WrittenTarget]:
    """The files the git configuration names for git to write to, as git reads them with the session's environment,
    include.path followed: each trace2 target the system and global configuration name (TRACE2_SETTINGS), but for one
    whose variable holds a value, which outranks the setting; and, from every scope the guard's own checkout reads, the
    hub's included, each cookie jar an http cookieFile setting names while an http saveCookies setting is on, and the
    folder fsmonitor.socketDir names. The guard's own git runs with every trace off, so this lookup writes no trace
    either. A relative path a CONFIGURATION_LOCATIONS variable holds is a target the guard cannot place, and so is a
    cookie jar or folder git reads from where it starts, start being the home a leading ~/ names.

    Raises GitError where git cannot read the configuration."""
    overridden = {variable for variable in TRACE2_SETTINGS.values() if variables.get(variable)}
    # A relative path among the variables that locate the system and global configuration is read from wherever each
    # git starts, never from the guard's own directory, so the targets the files there name cannot be placed.
    targets = [
        WrittenTarget(
            name,
            f"The session's environment holds {name}={variables[name]}",
            INHERITED_IT.format(name=name),
            None,
            "a relative path each git reads its configuration through from wherever it starts, which may name a "
            "trace2 target in a guarded hub",
        )
        for name in CONFIGURATION_LOCATIONS
        if variables.get(name) and placed_file(variables[name]) is None
    ]
    arguments = ["config", "--includes", "--show-scope", "--null", "--get-regexp", CONFIGURED_WRITES]
    # git reads an includeIf by where it runs, a gitdir or onbranch condition among them, so the settings are asked
    # from the guard's own checkout and from each place of the session's, its directory, its project, and each hub in
    # view; exit 1 with nothing on stderr is git finding no such setting.
    directories: list[Path] = []
    for directory in (Path(__file__).resolve().parent, *places):
        if worktrees.is_folder(directory) and not any(same_path(directory, each) for each in directories):
            directories.append(directory)
    fields: list[str] = []
    for directory in directories:
        output = CLIENT.quiet_output(arguments, directory=directory, timeout=lookup_timeout())
        # Each setting is its scope, then its name and value on two lines, each ended by a NUL.
        found = (output or "").split("\0")
        fields.extend(field for pair in zip(found[0::2], found[1::2], strict=False) for field in pair)
    entries = list(zip(fields[0::2], fields[1::2], strict=False))
    return targets + settings_targets(entries, overridden, start, "The session's {scope} git configuration")


def settings_targets(
    entries: Iterable[tuple[str, str]], overridden: Iterable[str], start: Path | None, holder: str
) -> list[WrittenTarget]:
    """The files the settings a git configuration listing gives, each its scope and its entry, name for git to write
    to (configured_written_targets): each trace2 target of the system or global scope, but for one whose variable
    overridden names, each cookie jar while a saveCookies setting is on, and the folder fsmonitor.socketDir names.
    holder names what sets them, its {scope} the setting's scope, as a refusal says it."""
    targets: list[WrittenTarget] = []
    cookie_jars: list[WrittenTarget] = []
    saving = False
    unplaced = "a path git reads from where each git starts, or expands for itself, which may lie in a guarded hub"
    # A setting with no value has no second line; one every checkout reads is judged once.
    seen: set[tuple[str, str]] = set()
    for scope, entry in entries:
        if (scope, entry) in seen:
            continue
        seen.add((scope, entry))
        setting, has_value, value = entry.partition("\n")
        named = f"{holder.format(scope=scope)} sets {setting} to {value}"
        if SAVE_COOKIES.fullmatch(setting):
            saving = saving or not has_value or value.strip().lower() not in FALSE_SETTINGS
        elif COOKIE_FILE.fullmatch(setting) and value and value not in DEVICE_FILES:
            remedy = CONFIGURED_WRITE_IT.format(name=setting)
            cookie_jars.append(
                WrittenTarget(setting, named, remedy, included_file(value, None, start), unplaced, COOKIE_WRITER)
            )
        elif setting == SOCKET_DIRECTORY and value:
            remedy = CONFIGURED_WRITE_IT.format(name=setting)
            targets.append(
                WrittenTarget(setting, named, remedy, included_file(value, None, start), unplaced, SOCKET_WRITER)
            )
        elif setting in TRACE2_SETTINGS:
            variable = TRACE2_SETTINGS[setting]
            if scope not in TRACE2_SCOPES or variable in overridden or not has_value:
                continue
            if value in DEVICE_FILES or not git_writes_trace_at(value):
                continue
            remedy = CONFIGURED_TRACE_IT.format(name=setting, variable=variable)
            targets.append(WrittenTarget(setting, named, remedy, placed_file(value)))
    # git writes a cookie jar back only while a saveCookies setting is on, which may be one for a URL of its own.
    return targets + (cookie_jars if saving else [])


@dataclass(frozen=True)
class RepositoryConfiguration:
    """What the configuration a git call reads where it runs holds for the guard (repository_configuration): the
    files its settings name for git to write to, and each file an include.path or includeIf.<condition>.path in it
    names, None for one the guard cannot place."""

    targets: list[WrittenTarget]
    includes: list[Path | None]


def repository_configuration(lookup: AliasLookup, start: Path | None) -> RepositoryConfiguration:
    """The configuration a git call reads at a place (alias_lookups), from every scope, its repository's own and a
    conditional include that holds only there among them, as git lists it with includes followed: the cookie jar and
    the socket folder its settings name (settings_targets), and the files its includes name, a relative one read from
    the directory of the file holding it, which a relative origin git lists names from the top of the work tree or the
    git directory it runs in, the nearest of lookup's directory and its parents that holds it, start being the home a
    leading ~/ names.

    Raises GitError where git cannot read the configuration, and OSError where the guard cannot look for an origin."""
    repository = () if lookup.repository is None else ("--git-dir", str(lookup.repository))
    arguments = [*repository, "config", "--includes", "--show-scope", "--show-origin", "--null", "--list"]
    output = CLIENT.quiet_output(arguments, directory=lookup.directory, timeout=lookup_timeout())
    fields = (output or "").split("\0")
    entries: list[tuple[str, str]] = []
    includes: list[Path | None] = []
    # Each setting is its scope, its origin, then its name and value on two lines, each ended by a NUL.
    for scope, origin, entry in zip(fields[0::3], fields[1::3], fields[2::3], strict=False):
        setting, has_value, value = entry.partition("\n")
        if re.fullmatch(CONFIGURED_WRITES, setting):
            entries.append((scope, entry))
        if has_value and INCLUDE_SETTING.fullmatch(setting):
            source = origin_file(origin, lookup.directory)
            includes.append(included_file(value, source, start))
    holder = f"The {{scope}} git configuration a git call in {lookup.directory} reads"
    return RepositoryConfiguration(settings_targets(entries, (), start, holder), includes)


def origin_file(origin: str, directory: Path) -> Path | None:
    """The file an origin git lists names, with every symbolic link followed: an absolute one as it is, and a relative
    one from the nearest of directory and its parents that holds it, where git read it from the top of the work tree
    or the git directory it moved to; None for an origin that is no file, as the command line, or that none holds.

    Raises OSError where a place on the way cannot be looked at, which may hold it."""
    if not origin.startswith("file:"):
        return None
    name = origin.removeprefix("file:")
    placed = placed_file(name)
    if placed is not None:
        return placed
    relative = Path(platform_support.from_shell_path(name))
    for each in (directory, *directory.parents):
        if file_presence.is_present(each / relative, other_is_absent=True):
            return Path(os.path.realpath(each / relative))
    return None


def included_file(value: str, source: Path | None, start: Path | None) -> Path | None:
    """The file an include.path or includeIf.<condition>.path setting names, as git reads it: a leading ~/ from the
    home start names, a relative path from the directory of the file source holding it; None where the guard cannot
    place it, as a ~user/ path or a %(prefix)/ path, which git expands for itself (interpolate_path)."""
    if value.startswith(("~/", "~\\")):
        return None if start is None else Path(os.path.realpath(start / value[2:]))
    if value.startswith(("~", "%(")):
        return None
    path = Path(platform_support.from_shell_path(value))
    if path.is_absolute():
        return Path(os.path.realpath(path))
    return None if source is None else Path(os.path.realpath(source.parent / path))


def configuration_files(start: Path | None) -> list[Path | None]:
    """The files git reads its system and global configuration from with the session's environment, the ones trace2
    reads its targets from: each `git var` names, whether or not it exists, every file the listing of those scopes
    names as an origin, and every file an include.path or includeIf.<condition>.path in them names, whether or not it
    exists. Each has every symbolic link followed; None is one the guard cannot place, start being the home a leading
    ~/ names.

    Raises GitError where git cannot name or read them."""
    directory = Path(__file__).resolve().parent
    files: list[Path | None] = []
    for variable in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        # Exit 1 with nothing on stderr is git reading no such file, as with GIT_CONFIG_NOSYSTEM.
        output = CLIENT.quiet_output(["var", variable], directory=directory, timeout=lookup_timeout())
        files += [placed_file(line) for line in output_lines(output or "")]
    arguments = ["config", "--includes", "--show-scope", "--show-origin", "--null", "--list"]
    fields = (CLIENT.quiet_output(arguments, directory=directory, timeout=lookup_timeout()) or "").split("\0")
    # Each setting is its scope, its origin, then its name and value on two lines, each ended by a NUL.
    for scope, origin, entry in zip(fields[0::3], fields[1::3], fields[2::3], strict=False):
        if scope not in TRACE2_SCOPES:
            continue
        source = placed_file(origin.removeprefix("file:")) if origin.startswith("file:") else None
        files.append(source)
        setting, has_value, value = entry.partition("\n")
        if has_value and INCLUDE_SETTING.fullmatch(setting):
            files.append(included_file(value, source, start))
    return files


def withhold_written_variables(environment: MutableMapping[str, str]) -> None:
    """Removes from environment each variable naming a file git writes its trace or output to (written_targets), and
    turns off each trace2 target (TRACE2_SETTINGS), which outranks one the configuration names, so the guard's own git
    calls, which run with this process's environment, write no trace or output anywhere; the guard judges the calls a
    command makes from the environment it was started with."""
    for name in list(environment):
        canonical = folded(name)
        if TRACE_VARIABLE.fullmatch(canonical) or canonical in REDIRECT_VARIABLES:
            del environment[name]
    environment.update(dict.fromkeys(TRACE2_SETTINGS.values(), "0"))


def shared_store_hub(place: Place) -> tuple[Path | None, bool]:
    """The guarded hub whose store, its shared refs among it, a call at place writes as its own: the hub's, for a call
    in the hub or in one of its linked worktrees (own_paths); and whether the text, or git, does not settle it."""
    if (place.start or place.tree) is None or (place.repository_named and place.repository is None):
        return None, True
    try:
        own = own_paths(place)
        return (None, False) if own is None else (guarded_hub_of_repository(own.common_dir), False)
    except GitError:
        return None, True  # a repository git cannot read may share a guarded hub's store


# The names of the files a repository's own git configuration is read from: its git directory's config, and a linked
# worktree's config.worktree.
REPOSITORY_CONFIGURATION_NAMES = frozenset({"config", "config.worktree"})


def configuration_file_writes(calls: Iterable[GitCall]) -> list[Path]:
    """The files the git config calls among calls write by --file, a relative one read from where git starts: a
    configuration a later git call may read, as a write by a redirection is."""
    paths: list[Path] = []
    for call in calls:
        if call.program != "git" or call.subcommand != "config":
            continue
        reads, file = config_reads(call.arguments)
        if reads or file is None:
            continue
        path = Path(platform_support.from_shell_path(file))
        if path.is_absolute() or call.place.start is not None:
            paths.append(path if path.is_absolute() else (call.place.start or Path()) / path)
    return paths


@dataclass
class Judge:
    """Judges a reading's calls against the guarded hubs in view of the session."""

    hubs: set[Path]
    environment: Mapping[str, str]
    notes: list[str]
    deadline: float
    # The files git reads its system and global configuration from (configuration_files), once a write asks for them.
    configuration: list[Path | None] | None = None

    def denied(self, reading: Reading, depth: int = 0) -> str | None:
        if reading.refusals:
            if self.hubs:
                return OwnRemedy(reading.refusals[0])
            self.notes.extend(reading.refusals)
        unread = list(reading.unread)
        writes = list(reading.writes)
        for run in reading.scripts:
            reading.time_left()
            if not is_trusted_script(run.script):
                unread.extend(what for what in run.unread if what not in unread)
                writes.extend(run.writes)
        if unread:
            if self.hubs:
                return (
                    f"The guard does not read part of this command ({unread[0]}), so it is refused while a "
                    f"hub is guarded. {REWRITE_IT}"
                )
            self.notes.extend(unread)
        for write in writes:
            reading.time_left()
            reason = self.write_refusal(write)
            if reason is not None:
                return reason
        for call in reading.calls:
            reading.time_left()
            reason = self.judge(call, depth)
            if reason is not None:
                return reason
        return self.repository_refusal(reading, [write.path for write in writes])

    def repository_refusal(self, reading: Reading, written: list[Path]) -> str | None:
        """Why a command is refused for the configuration its git calls read where they run (repository_configuration),
        which the session's own places do not show: a cookie jar or a socket folder it names in a guarded hub, which
        a git call there writes, or, while a hub is guarded, one the guard cannot place or a configuration git cannot
        read. The guard reads each configuration as it stands when the command starts, so while a hub is guarded it
        also refuses a command that writes a file an include in one names, and one that runs git outside every guarded
        hub, where the guard does not know which repository's configuration the command makes or writes, and writes a
        file named config or config.worktree, or one in a folder named .git, as a repository's own configuration is."""
        if not reading.calls:
            return None
        written = written + configuration_file_writes(reading.calls)
        session = [Path(__file__).resolve().parent, *reading.places, *self.hubs]
        seen: list[AliasLookup] = []
        foreign = False
        for call in reading.calls:
            start = call.place.start or call.place.tree
            places = [start, call.place.repository] if call.place.repository_named else [start]
            # A git config that writes reads no setting that has git write a file; its --file is among the writes.
            outside = any(place is None or not any(within(place, hub) for hub in self.hubs) for place in places)
            foreign = foreign or (outside and not configuration_file_writes([call]))
            try:
                lookups = alias_lookups(call.place)
            except GitError as exc:
                lookups = []
                if self.hubs:
                    return f"The guard could not read where {call_named(call)} runs ({exc}), so it is refused."
                self.notes.append(f"could not read where {call_named(call)} runs")
            for lookup in lookups:
                if lookup in seen:
                    continue
                seen.append(lookup)
                reason = self.lookup_refusal(call, lookup, session, written)
                if reason is not None:
                    return reason
        if not (foreign and self.hubs):
            return None
        for path in written:
            # A file system that folds case reads Config or .GIT as git's own.
            parts = {name_key(part) for part in path.parts}
            if name_key(path.name) in REPOSITORY_CONFIGURATION_NAMES or name_key(".git") in parts:
                return (
                    f"The command writes {path}, which may be a repository's own git configuration, and runs git "
                    "outside every guarded hub, where a setting written there, such as a cookie jar in a hub, reaches "
                    "the git the rest of the command starts, though the guard read the configuration before the "
                    "command ran, so it is refused while a hub is guarded. Write the configuration in a command of its "
                    "own."
                )
        return None

    def lookup_refusal(
        self, call: GitCall, lookup: AliasLookup, session: Sequence[Path], written: Sequence[Path]
    ) -> str | None:
        """Why a command is refused for the configuration one git call reads at one place (repository_refusal): a target
        it names, judged where the session's places did not already judge it, as session_written_refusal judges one;
        and an include whose file, or a folder holding it, the command writes, or that the guard cannot place while the
        command writes any."""
        judged = lookup.repository is None and any(same_path(lookup.directory, place) for place in session)
        if judged and not (written and self.hubs):
            return None
        try:
            configuration = repository_configuration(lookup, home(self.environment))
        except (GitError, OSError) as exc:
            if self.hubs:
                return (
                    f"The guard could not read the git configuration {call_named(call)} reads in {lookup.directory} "
                    f"({exc}), which may name a file git writes in a guarded hub, so it is refused while a hub is "
                    f"guarded. {UNREAD_CONFIGURATION_IT}"
                )
            self.notes.append(f"could not read the git configuration {call_named(call)} reads")
            return None
        for target in [] if judged else configuration.targets:
            if target.path is None:
                if self.hubs:
                    return (
                        f"{target.named}, {target.unplaced}, and {target.writer} would write there, so the command is "
                        f"refused while a hub is guarded. {target.remedy}"
                    )
                self.notes.append(f"could not tell where {target.source} has git write")
            elif hub_protected(target.path):
                return (
                    f"{target.named}, and {target.writer} writes there, into a file of a guarded hub, so the command "
                    f"is refused. {target.remedy}"
                )
        if not (written and self.hubs):
            return None
        for include in configuration.includes:
            if include is None:
                return (
                    f"The git configuration {call_named(call)} reads in {lookup.directory} includes a file the guard "
                    "cannot place, which the command may write, so it is refused while a hub is guarded. "
                    f"{UNREAD_CONFIGURATION_IT}"
                )
            # A folder a write names holds what a copy into it, a move, or a removal reaches.
            if any(within(include, Path(os.path.realpath(path))) for path in written):
                return (
                    f"The command writes {include}, a file the git configuration {call_named(call)} reads in "
                    f"{lookup.directory} includes, and a setting written there, such as a cookie jar in a hub, reaches "
                    "that git though the guard read the configuration before the command ran, so it is refused while "
                    "a hub is guarded. Write the configuration in a command of its own."
                )
        return None

    def judge(self, call: GitCall, depth: int) -> str | None:
        reason = self.index_refusal(call) or self.environment_refusal(call) or self.effects_refusal(call)
        if reason is not None:
            return reason
        if call.subcommand is None and self.hubs:
            # Whatever place it runs in, a subcommand the text does not settle may be an alias that writes a hub.
            return "The guard could not tell which git subcommand runs, so it is refused while a hub is guarded."
        hub, unknown = reached_hub(call.place)
        if hub is None and not (unknown and self.hubs):
            if unknown and hub_refusal(call) is not None:
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
        reason = hub_refusal(call)
        if reason is None and call.program == "git" and call.subcommand == "merge":
            reason = next(filter(None, map(shadowed_fast_forward, [hub] if hub else sorted(self.hubs))), None)
        if reason is not None and may_be_alias(call):
            # No read-only form names it, but an alias of that name may run one: the alias is judged by what it runs,
            # and a name that is no alias stays refused.
            hubs = [hub] if hub is not None else sorted(self.hubs)
            reason = self.alias_refusal(call, [AliasLookup(each, None, each) for each in hubs], depth, missing=reason)
        if reason is None or hub is not None:
            return reason
        return (
            f"The guard could not tell where `git {call.subcommand}` runs; since it cannot place it while a hub is "
            f"guarded, it allows it only in the hub's read-only forms: {reason} {PLACE_IT}"
        )

    def index_refusal(self, call: GitCall) -> str | None:
        """Why a call is refused for the index GIT_INDEX_FILE names (index_reached): every git call, wherever it runs
        and whatever its subcommand, while that index is one a guarded hub holds and not a file in the call's own
        linked worktree's git directory, since a form
        that only reads the hub, status or a fast-forward among them, still refreshes or rewrites the index it is
        given, out of step with the hub's HEAD; and, while a hub is guarded, every one whose index the guard cannot
        place."""
        hub, unknown = index_reached(call.place)
        named = call_named(call)
        if hub is not None:
            return (
                f"{named} would run with GIT_INDEX_FILE={call.place.index}, an index the guarded hub {hub} holds, and "
                f"any git call, or a git it starts in another checkout, may rewrite the index it is given. {INDEX_IT}"
            )
        if unknown and self.hubs:
            return (
                f"{named} would run with a GIT_INDEX_FILE the guard cannot place, a relative path among them, which "
                f"may be a guarded hub's, so it is refused while a hub is guarded. {INDEX_IT}"
            )
        if unknown:
            self.notes.append(f"could not tell where the index of {named} lies")
        return None

    def session_written_refusal(self, places: Iterable[Path] = ()) -> str | None:
        """Why every command is refused for a file the session's environment or configuration has git write: its
        trace or its output, or the keys of a TLS session, where an inherited variable names it (written_targets), or
        a trace2 target, a cookie jar, or the folder of a file-system monitor's socket, where the configuration names
        it (configured_written_targets). The environment and the configuration reach every git the command starts, a
        program's own and a `gh` subcommand's among them, which the text does not show, and no git call is ever owed
        such a file in a hub, so one that lies in a guarded hub refuses the command whatever it holds; one the guard
        cannot place, or a configuration git cannot read, is refused while a hub is guarded and otherwise noted."""
        variables = {folded(name): value for name, value in self.environment.items()}
        targets = written_targets(variables)
        try:
            targets += configured_written_targets(variables, home(self.environment), [*places, *sorted(self.hubs)])
        except GitError as exc:
            if self.hubs:
                return (
                    f"The guard could not read the files the git configuration names for git to write ({exc}), and "
                    "every git a command starts would write to them, so every command is refused while a hub is "
                    f"guarded. {UNREAD_CONFIGURATION_IT}"
                )
            self.notes.append("could not read the files the git configuration names for git to write")
        for target in targets:
            if target.path is None:
                if self.hubs:
                    return (
                        f"{target.named}, {target.unplaced}, and {target.writer} would write there, so every command "
                        f"is refused while a hub is guarded. {target.remedy}"
                    )
                self.notes.append(f"could not tell where {target.source} has git write")
            elif hub_protected(target.path):
                return (
                    f"{target.named}, and {target.writer} writes there, into a file of a guarded hub, so every "
                    f"command is refused while it does. {target.remedy}"
                )
        return None

    def environment_refusal(self, call: GitCall) -> str | None:
        """Why a call is refused for where the inherited environment sends what git writes beyond a checkout's HEAD,
        index, and tree; a trace or output file is judged for the whole command (session_written_refusal). An object
        store or common directory (STORE_VARIABLES) other than the call's own (own_paths) is refused where a guarded
        hub holds it, since git writes its objects, configuration, or refs there. A call that acts on a guarded hub
        (reached_hub) runs only with the hub's own index, store, and common directory, and with the hub's own view of
        its objects: given any GIT_INDEX_FILE, another store, or one of OBJECT_VIEW_VARIABLES, a fast-forward moves
        the hub's HEAD and tree while the index it writes lies elsewhere, or its refs to objects its store lacks. And
        a call whose repository shares a guarded hub's store (shared_store_hub), in one of its linked worktrees,
        writes the refs that store holds, so it runs only with that store and that view of it. Where the guard cannot
        place one of these, it is refused while a hub is guarded and otherwise noted."""
        named = call_named(call)
        variables = {folded(name): value for name, value in self.environment.items()}
        foreign = [name for name in OBJECT_VIEW_VARIABLES if name in variables]
        for name, what in STORE_VARIABLES.items():
            if name not in variables:
                continue
            hub, unknown, own = store_reached(call.place, name, variables[name])
            if hub is not None:
                return (
                    f"{named} would run with {name}={variables[name]}, in the guarded hub {hub}, where git would write "
                    f"its {what}. {INHERITED_IT.format(name=name)}"
                )
            if unknown and self.hubs:
                return (
                    f"{named} would run with {name}={variables[name]}, a store the guard cannot place, which may be a "
                    f"guarded hub's, so it is refused while a hub is guarded. {INHERITED_IT.format(name=name)}"
                )
            if unknown:
                self.notes.append(f"could not tell where {name} has {named} write")
            if not own:
                foreign.append(name)
        given = ["GIT_INDEX_FILE", *foreign] if call.place.index_named else foreign
        if not given:
            return None
        hub, unknown = reached_hub(call.place)
        if hub is not None or (unknown and self.hubs):
            where = (
                f"the guarded hub {hub}" if hub is not None else "a place the guard cannot settle, which may be a hub"
            )
            return (
                f"{named} would act on {where} given {', '.join(given)} from the session's environment: git would "
                "move the hub's HEAD, tree, or refs while the index it writes lies elsewhere, or with a view of "
                "objects other than its store's, leaving the hub's own index out of step or its refs naming objects "
                f"its store lacks. {INHERITED_IT.format(name=given[0])}"
            )
        if not foreign:
            return None
        shared, unknown = shared_store_hub(call.place)
        if shared is None and not (unknown and self.hubs):
            return None
        where = (
            f"the refs the guarded hub {shared} shares with its worktrees"
            if shared is not None
            else "refs a guarded hub may share, from a place the guard cannot settle"
        )
        return (
            f"{named} would write {where} given {', '.join(foreign)} from the session's environment: git may move "
            "them to objects the hub's store lacks, or prune objects they still need. "
            f"{INHERITED_IT.format(name=foreign[0])}"
        )

    def effects_refusal(self, call: GitCall) -> str | None:
        """Why what a git call's words make it do is refused, wherever it runs: a file it writes at a path that lies in
        a guarded hub, judged as a redirection's target is; and, while a hub is guarded, a command it runs that its
        words name, a file it writes where the text does not settle, and a word the text does not settle given to a
        subcommand with an option that does either. A git config that may write is judged by config_refusal."""
        subcommand = call.subcommand
        if call.program != "git" or subcommand is None:
            return None
        if subcommand == "config":
            return self.config_refusal(call)
        effects = git_effects(subcommand, call.arguments)
        if effects.runs is not None and self.hubs:
            return (
                f"`git {subcommand} {effects.runs}` runs a command its words name, which may be git, so it is refused "
                "while a hub is guarded."
            )
        writes = GIT_OUTPUT_SUBCOMMANDS | GIT_WRITING_OPTIONS.keys() | GIT_RUNNING_OPTIONS.keys() | PLAIN_ACTIONS.keys()
        writes |= GIT_DEFAULT_WRITES | GIT_OPERAND_SUBCOMMANDS | GIT_UNREAD_MODES.keys()
        if effects.unsettled and self.hubs and subcommand in writes:
            return (
                f"`git {subcommand}` is given a word the text does not settle, which may be an option that writes a "
                "file or runs a command, so it is refused while a hub is guarded."
            )
        for value in effects.written:
            reason = self.written_refusal(call, value)
            if reason is not None:
                return reason
        return None

    def written_refusal(self, call: GitCall, value: Word) -> str | None:
        """Why a file a git call writes at a path its words name is refused: one in a guarded hub's tracked files or
        anywhere in its git directory, as a redirection's target is, a relative path read from where git starts; and,
        while a hub is guarded, one whose path or directory the text does not settle."""
        if value in DEVICE_FILES:
            return None
        unsettled = f"`git {call.subcommand}` writes a file the text does not settle, which may lie in a hub."
        if value is None:
            return unsettled if self.hubs else None
        path = Path(platform_support.from_shell_path(value))
        if not path.is_absolute():
            if call.place.start is None:
                return unsettled if self.hubs else None
            path = call.place.start / path
        if hub_protected(path):
            return f"`git {call.subcommand}` writes {path}, a file of a guarded hub."
        return self.configuration_write_refusal(path, f"`git {call.subcommand}` writes")

    def write_refusal(self, write: Write) -> str | None:
        """Why a file a redirection, a program, or a cmdlet writes at a path the text names is refused: one in a guarded
        hub's tracked files or anywhere in its git directory, a link followed, and, while a hub is guarded, one git
        reads its system or global configuration from. For a pattern, the folder its matches lie in is judged: one in
        a guarded hub, one holding a guarded hub in view, and, while a hub is guarded, one holding a file git reads
        its configuration from."""
        pattern = write.pattern and not write.folder
        opening = f"{write.opening} the files a pattern matches in" if pattern else write.opening
        if hub_protected(write.path):
            what = "a folder" if write.pattern or write.folder else "a file"
            return f"{opening} {write.path}, {what} of a guarded hub."
        # A folder holds what a recursive removal, a move, a mode change, or a copy into it reaches.
        folder = Path(os.path.realpath(write.path))
        hub = next((hub for hub in sorted(self.hubs) if within(hub, folder)), None)
        if hub is not None:
            return f"{opening} {write.path}, which holds the guarded hub {hub}."
        return self.configuration_write_refusal(write.path, opening)

    def configuration_write_refusal(self, path: Path, writer: str) -> str | None:
        """Why a file a command writes is refused, while a hub is guarded, for being one git reads its system or global
        configuration from (configuration_files), or a folder holding one: a trace2 target or another setting written
        there reaches every git the rest of the command starts, a program's own included, though the guard judged the
        configuration before the command ran. Where git cannot name those files, or the guard cannot place one, every
        file a command writes is refused while a hub is guarded."""
        if not self.hubs:
            return None
        if self.configuration is None:
            try:
                self.configuration = configuration_files(home(self.environment))
            except GitError as exc:
                return (
                    f"{writer} {path}, and the guard could not tell which files git reads its system and global "
                    f"configuration from ({exc}), so it is refused while a hub is guarded. {UNREAD_CONFIGURATION_IT}"
                )
        if None in self.configuration:
            return (
                f"{writer} {path}, and the guard cannot place a file git reads its system or global configuration "
                f"from, which may be this one, so it is refused while a hub is guarded. {UNREAD_CONFIGURATION_IT}"
            )
        written = Path(os.path.realpath(path))
        files = [file for file in self.configuration if file is not None]
        if not any(same_path(written, file) for file in files) and any(within(file, written) for file in files):
            return (
                f"{writer} {path}, which holds a file git reads its system or global configuration from, and a "
                "setting written there, such as a trace2 target in a hub, reaches every git the rest of the command "
                "starts, so it is refused while a hub is guarded. Change the git configuration from a terminal."
            )
        if any(same_path(written, file) for file in files):
            return (
                f"{writer} {path}, a file git reads its system or global configuration from, and a setting written "
                "there, such as a trace2 target in a hub, reaches every git the rest of the command starts, so it is "
                "refused while a hub is guarded. Change the git configuration from a terminal."
            )
        return None

    def config_refusal(self, call: GitCall) -> str | None:
        """Why a git config is refused: one that only reads never is (config_reads); one that writes the file --file
        names is judged as any file a git call writes; and every other one that may write is refused while a hub is
        guarded, since the configuration a hub shares with its worktrees holds the guard's own opt-in, and the user's
        and the system's hold settings that run a command in the hub."""
        reads, file = config_reads(call.arguments)
        if reads:
            return None
        if file is not None:
            return self.written_refusal(call, file)
        if not self.hubs:
            return None
        return (
            "`git config` that may write a setting is refused while a hub is guarded: the configuration a hub shares "
            "with its worktrees holds the guard's own opt-in, and a setting may run a command. Change it from a "
            "terminal."
        )

    def unreadable(self, call: GitCall, why: str) -> str | None:
        """An alias that cannot be read is refused while a guarded hub is in view, unlike a command whose text cannot
        be: it hides its subcommand. With none in view it is allowed with a note."""
        message = f"The alias `git {call.subcommand}` could not be read ({why})"
        if self.hubs:
            return f"{message}, so it is refused."
        self.notes.append(message)
        return None

    def alias_refusal(
        self, call: GitCall, lookups: list[AliasLookup], depth: int, missing: str | None = None
    ) -> str | None:
        """Why the alias a call names would write a hub; None when it is one that would not, and missing when it is no
        alias at all.

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
        found = False
        for lookup in lookups:
            repository = () if lookup.repository is None else ("--git-dir", str(lookup.repository))
            result = git(lookup.directory, *repository, "config", "--get", f"alias.{subcommand}")
            if result.returncode == 1:
                continue
            if result.returncode != 0:
                arguments = [*repository, "config", "--get", f"alias.{subcommand}"]
                return self.unreadable(call, flat_text(failure_message(arguments, result)))
            found = True
            reason = self.expansion_refusal(call, result.stdout.removesuffix("\n"), depth, lookup.top)
            if reason is not None:
                return reason
        return None if found else missing

    def expansion_refusal(self, call: GitCall, expansion: str, depth: int, top: Path | None) -> str | None:
        """Why what an alias expands to would write a hub; a shell alias runs at top, the top of the work tree."""
        if depth >= NESTING_LIMIT:
            return self.unreadable(call, f"it nests more than {NESTING_LIMIT} deep")
        reading = Reading(self.environment, expansion, deadline=self.deadline)
        if expansion.startswith("!"):
            if None in call.arguments:
                return self.unreadable(call, "a word after it is one the text does not settle")
            # git runs a shell alias at the top of the work tree, with the call's arguments after it.
            script = " ".join([expansion[1:], *(shlex.quote(argument or "") for argument in call.arguments)])
            reading.text = script
            read_bash(script, top, reading, depth + 1)
        else:
            try:
                words: list[Word] = list(shlex.split(expansion))
            except ValueError as exc:
                return self.unreadable(call, str(exc))
            options = call.options.copy() if call.options is not None else GitOptions.initial(None, {}, opaque=True)
            record_git_call([*words, *call.arguments], options, reading)
        reason = Judge(self.hubs, self.environment, [], self.deadline).denied(reading, depth + 1)
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
    """Whether a call's subcommand may be an alias: one neither READ_ONLY_FORMS names nor git lists among its builtins,
    which no alias can stand for."""
    return (
        call.program == "git"
        and call.subcommand is not None
        and call.subcommand not in READ_ONLY_SUBCOMMANDS | git_builtins()
    )


def alias_lookups(place: Place) -> list[AliasLookup]:
    """Where git reads the aliases of a call at a settled place: the configuration of the repository it uses, found
    from where it starts or named, which a worktree shares with its hub. A place that does not exist runs nothing; one
    that cannot be looked at raises GitError, as a place git cannot read does, never reads as one that runs nothing."""
    directory = place.start or place.tree
    if directory is None or not worktrees.is_folder(directory):
        return []
    checkout = checkout_of(place.tree) if place.tree is not None and place.tree == place.start else None
    top = checkout.toplevel if checkout is not None else place.tree
    return [AliasLookup(directory, place.repository if place.repository_named else None, top)]


def denied(reading: Reading, hubs: Iterable[Path] = (), depth: int = 0) -> str | None:
    """Why a reading's calls would write a guarded hub, or None; notes on what was passed over join the reading's."""
    judge = Judge(set(hubs), reading.environment, reading.notes, reading.deadline)
    with lookups_within(reading.deadline):
        return judge.session_written_refusal(reading.places) or judge.denied(reading, depth)


# ---------------------------------------------------------------------------------------------------------------
# Where a shell is


@dataclass(frozen=True)
class Directory:
    """Where a shell is, as far as the text settles it, and its pushd stack; None is a directory it does not."""

    cwd: Path | None
    stack: tuple[Path | None, ...] = ()


UNKNOWN = Directory(None)


def joined(first: Directory, second: Directory) -> Directory:
    """A shell that may be in either directory: what the two agree on stays settled, and the rest is not."""
    cwd = first.cwd if same_place(first.cwd, second.cwd) else None
    if len(first.stack) == len(second.stack) and all(map(same_place, first.stack, second.stack)):
        return Directory(cwd, first.stack)
    return Directory(cwd, (None,) * max(len(first.stack), len(second.stack)))


@dataclass(frozen=True)
class Outcome:
    """Where a shell is after a command that succeeded, and after one that failed, which && and || go on from."""

    ok: Directory
    fail: Directory

    def merged(self) -> Directory:
        return joined(self.ok, self.fail)


def certain(directory: Directory) -> Outcome:
    return Outcome(directory, directory)


def moved_to(directory: Directory, target: Path, stack: tuple[Path | None, ...]) -> Outcome:
    """A directory change to target: where it succeeds, the shell is there; a target that is no directory now fails,
    which leaves the shell where it was, unless an earlier command makes it one."""
    path = Path(os.path.normpath(target))
    arrived = Directory(path, stack)
    return certain(arrived) if path.is_dir() else Outcome(arrived, directory)


def home(environment: Mapping[str, str]) -> Path | None:
    """The home a shell started with `environment` takes `~` and a bare cd to: its HOME, else the variable Python's
    home lookup reads, or None where neither names an absolute path, so the place is left unknown rather than read
    relative to the guard's own working directory."""
    value = environment.get("HOME")
    if value:
        path = Path(platform_support.from_shell_path(value))
        return path if path.is_absolute() else None
    return executable_locator.environment_path(executable_locator.HOME_VARIABLE, environment)


def free_name(name: str, environment: Mapping[str, str]) -> bool:
    """Whether a variable's value is one nothing the guard reads takes from the shell: a name Bash neither reads nor
    sets, or one it reads for what changes nothing the guard reads, that git does not read, and that the environment
    does not hold, so that assigning it reaches no program."""
    held = any(each == name for each in environment)
    return bool(bash_syntax.NAME.fullmatch(name)) and harmless_name(name) and not held


def harmless_name(name: str) -> bool:
    """Whether a variable is one an assignment before a program may give it: one git does not read to choose a file or
    a program (no GIT_ name, none GIT_READ_NAMES names, and none the loader reads, LOADER_PREFIXES), and Bash does not
    read for what the guard reads."""
    canonical = folded(name)
    return (
        BASH_VARIABLES.get(name, "") == ""
        and not canonical.startswith(("GIT_", *LOADER_PREFIXES))
        and canonical not in GIT_READ_NAMES
        and not EXPORTED_FUNCTION.match(canonical)
    )


# ---------------------------------------------------------------------------------------------------------------
# The Bash reader


@dataclass(frozen=True)
class BashStart:
    """What a bash takes from the environment it starts with that the reader needs: its home, whether it searches
    CDPATH, the functions it is handed by name, and whether xtrace may run what PS4 holds."""

    home: Path | None  # None where the home is not known, so a word or a directory holding it is unsettled
    cdpath: bool
    functions: frozenset[str]
    expanding_ps4: bool


def bash_start(reading: Reading) -> BashStart:
    """Reads the shell state the hook's environment hands the Bash tool's shell, and tells the reading of each
    option it turns on that the tables do not read."""
    environment = reading.environment
    expanding_ps4 = any(mark in environment.get("PS4", "") for mark in "$`")
    for variable, table in (("SHELLOPTS", "set"), ("BASHOPTS", "shopt")):
        for option in filter(None, environment.get(variable, "").split(":")):
            judge_option(reading, table, option, True, f"the {variable} the environment holds")
            if table == "set":
                judge_xtrace(reading, option, True, expanding_ps4)
    for variable, option in (("POSIXLY_CORRECT", "posix"), ("BASH_COMPAT", "compat44")):
        if variable in environment:
            table = "set" if option == "posix" else "shopt"
            judge_option(reading, table, option, True, f"the {variable} the environment holds")
    functions = frozenset(
        folded(match.group(1)) for match in (EXPORTED_FUNCTION.match(folded(name)) for name in environment) if match
    )
    if folded("command_not_found_handle") in functions:
        reading.cannot_read(
            "the environment hands the shell command_not_found_handle, which runs for any command it does not find"
        )
    return BashStart(home(environment), bool(environment.get("CDPATH")), functions, expanding_ps4)


def judge_xtrace(reading: Reading, option: str | None, on: bool, expanding_ps4: bool) -> None:
    """The one check of xtrace, whatever turns it on, set, shopt -o, or the environment's SHELLOPTS: refused where
    the environment's PS4 holds an expansion, which xtrace runs before each command."""
    if option == "xtrace" and on and expanding_ps4:
        reading.cannot_read(NOT_READ_XTRACE)


def judge_option(reading: Reading, table: str, option: str | None, on: bool, source: str) -> None:
    """Tells the reading of an option turned on or off that the tables do not read a shell under."""
    if option is None:
        reading.cannot_read(f"an option {source} turns on or off that the text does not settle")
        return
    state = (SET_OPTIONS.get(option, ("", "unknown"))[1]) if table == "set" else SHOPT_OPTIONS.get(option, "unknown")
    if state == "unknown":
        reading.cannot_read(
            f"the option {option}, which {source} turns {'on' if on else 'off'}, is one the guard does not know"
        )
    elif state == ("on" if on else "off"):
        reason = NOT_READ[f"set -o {option}" if table == "set" else f"shopt {option}"]
        reading.cannot_read(f"{source} turns {option} {state}: {reason}")


class BashReader:
    """Reads a Bash command tree for the git calls it makes and where each acts, and tells the reading of each part
    the guard does not read.

    Only a list of simple commands is placed exactly: a command that runs only when another succeeds or fails goes on
    from that outcome, a subshell, a pipeline's commands, and a background job run in a copy whose changes die with it,
    and a compound command that changes the directory leaves every git call in it, and every one after it, in a
    directory the text does not settle.
    """

    def __init__(self, reading: Reading, depth: int, start: BashStart) -> None:
        self.reading = reading
        self.depth = depth
        self.start = start

    def nested(self) -> BashReader:
        return BashReader(self.reading, self.depth + 1, self.start)

    def read_text(self, text: str, directory: Directory) -> Directory:
        if self.depth > NESTING_LIMIT:
            self.reading.cannot_read(f"substitutions nest more than {NESTING_LIMIT} deep")
            return UNKNOWN
        try:
            tree = bash_syntax.parse(text)
        except bash_syntax.BashSyntaxError as exc:
            self.reading.cannot_read(f"Bash would not read it as written, or the guard cannot ({exc})")
            return UNKNOWN
        return self.read_list(tree, directory)

    def read_list(self, node: bash_syntax.CommandList, directory: Directory) -> Directory:
        for and_or, background in node.items:
            outcome = self.read_and_or(and_or, directory)
            if not background:
                directory = outcome.merged()
        return directory

    def read_and_or(self, node: bash_syntax.AndOr, directory: Directory) -> Outcome:
        outcome = self.read_pipeline(node.first, directory)
        for operator, pipeline in node.rest:
            if operator == "&&":
                after = self.read_pipeline(pipeline, outcome.ok)
                outcome = Outcome(after.ok, joined(outcome.fail, after.fail))
            else:
                after = self.read_pipeline(pipeline, outcome.fail)
                outcome = Outcome(joined(outcome.ok, after.ok), after.fail)
        return outcome

    def read_pipeline(self, node: bash_syntax.Pipeline, directory: Directory) -> Outcome:
        if len(node.commands) == 1:
            outcome = self.read_command(node.commands[0], directory)
        else:
            for command in node.commands:
                self.read_command(command, directory)
            outcome = certain(directory)
        return Outcome(outcome.fail, outcome.ok) if node.negated else outcome

    def read_command(self, node: bash_syntax.Command, directory: Directory) -> Outcome:
        self.reading.time_left()
        if isinstance(node, bash_syntax.Simple):
            return self.read_simple(node, directory)
        if isinstance(node, bash_syntax.FunctionDefinition):
            self.reading.cannot_read(NOT_READ["construct function definition"])
            return certain(UNKNOWN)
        self.read_redirects(node.redirects, directory)
        if isinstance(node, bash_syntax.Subshell) and arithmetic_command(node):
            self.reading.cannot_read(NOT_READ["construct arithmetic command"])
        moves = changes_directory(node)
        inside = UNKNOWN if moves else directory
        if isinstance(node, bash_syntax.Loop):
            if node.header:
                self.reading.cannot_read(NOT_READ["construct arithmetic for"])
            if node.name is not None and not free_name(node.name, self.reading.environment):
                self.reading.cannot_read(
                    f"a loop assigns {node.name}, which Bash or git reads or the environment holds"
                )
            self.read_words(node.words or [], directory)
        if isinstance(node, bash_syntax.Case):
            self.read_words([node.word, *node.patterns], directory)
        for body in command_bodies(node):
            self.read_list(body, inside)
        if moves and not isinstance(node, bash_syntax.Subshell):
            return certain(UNKNOWN)
        return certain(directory)

    def read_words(self, words: Iterable[bash_syntax.Word], directory: Directory) -> list[Word]:
        """Each word's value, None where the text does not settle it, once every substitution in it is read."""
        values = []
        for word in words:
            for text in word.substitutions:
                self.nested().read_text(text, directory)
            values.append(self.word_value(word))
        return values

    def word_value(self, word: bash_syntax.Word) -> Word:
        """The text Bash makes of a word, or None where only running settles it; tells the reading of each part of
        it the guard does not read."""
        for name in word.assigns:
            self.read_assigned(name)
        pieces: list[str] = []
        settled = True
        for part in word.parts:
            if part.kind == "text":
                if not part.quoted and "$[" in part.text:
                    self.reading.cannot_read(NOT_READ["expansion $["])
                if not part.quoted and (GLOB.search(part.text) or BRACE.search(part.text)):
                    settled = False
                pieces.append(part.text)
            elif part.kind == "home" and self.start.home is not None:
                pieces.append(str(self.start.home))
            elif part.kind in ("home", "parameter"):
                settled = False
            else:
                settled = False
                self.read_dynamic(part.text)
        if not settled:
            return None
        value = "".join(pieces)
        self.reading.named.extend(named_paths(value))
        return value

    def read_assigned(self, name: str) -> None:
        if name.startswith("!") or "[" in name or not free_name(name, self.reading.environment):
            self.reading.cannot_read(f"{NOT_READ['expansion assigning']} ({name})")

    def read_dynamic(self, form: str) -> None:
        """Tells the reading of an expansion of a form the guard does not read."""
        key = expansion_form(form)
        if key not in EXPANSIONS_UNDERSTOOD:
            reason = NOT_READ.get(f"expansion {key}", "an expansion the guard does not know")
            self.reading.cannot_read(f"{reason} ({form})")

    def read_redirects(self, redirects: list[bash_syntax.Redirect], directory: Directory) -> None:
        for redirect in redirects:
            if redirect.descriptor.startswith("{"):
                self.reading.cannot_read(f"{NOT_READ['construct named descriptor']} ({redirect.descriptor})")
            here = redirect.here
            if here is not None:
                for text in here.substitutions:
                    self.nested().read_text(text, directory)
                for name in here.assigns:
                    self.read_assigned(name)
                for part in here.expansions:
                    self.read_dynamic(part.text)
                if not here.quoted and "$[" in here.body:
                    self.reading.cannot_read(NOT_READ["expansion $["])
                continue
            if redirect.target is None:
                continue
            value = self.read_words([redirect.target], directory)[0]
            if redirect.operator in {"<", "<<<"} or (
                redirect.operator in {"<&", ">&"} and value is not None and DESCRIPTOR_TARGET.fullmatch(value)
            ):
                continue
            read_written(self.target_value(redirect.target) if value is None else value, directory, self.reading)

    def target_value(self, word: bash_syntax.Word) -> Word:
        """A redirection target the text settles but for the system's temporary folder, named by a variable
        TEMPORARY_VARIABLES lists that the environment holds; None for any other, a pattern among them."""
        value, pattern = self.written_value(word)
        return None if pattern else value

    def written_value(self, word: bash_syntax.Word) -> tuple[Word, bool]:
        """A word a file is written at, read as a redirection target is (target_value), with whether it is a pattern:
        for a word whose unquoted text holds a wildcard, the folder every file it matches lies in (pattern_folder);
        (None, False) where the text does not settle it, a brace expansion among them, which may name any path."""
        pieces: list[str] = []
        for part in word.parts:
            if part.kind == "text" and not part.quoted:
                if BRACE.search(part.text):
                    return None, False
                wildcard = GLOB.search(part.text)
                if wildcard is not None:
                    return pattern_folder("".join(pieces) + part.text[: wildcard.start()]), True
                pieces.append(part.text)
            elif part.kind == "text":
                pieces.append(part.text)
            elif part.kind == "home" and self.start.home is not None:
                pieces.append(str(self.start.home))
            elif (
                part.kind == "parameter"
                and part.text in TEMPORARY_VARIABLES
                and self.reading.environment.get(part.text)
            ):
                pieces.append(self.reading.environment[part.text])
            else:
                return None, False
        return "".join(pieces), False

    def read_simple(self, node: bash_syntax.Simple, directory: Directory) -> Outcome:
        words = node.words
        values = self.read_words(words, directory)
        self.read_redirects(node.redirects, directory)
        index = 0
        while index < len(words) and words[index].assignment is not None:
            index += 1
        prefixes: list[tuple[str, Word]] = []
        for word in words[:index]:
            assignment = word.assignment
            if assignment is None or word.array:
                continue  # an array's value, NAME=( ), is a form word_value does not read
            if assignment.element:
                self.reading.cannot_read(NOT_READ["construct element assignment"])
            elif index == len(words) and not free_name(assignment.name, self.reading.environment):
                self.reading.cannot_read(f"assigns {assignment.name}, which Bash or git reads or the environment holds")
            else:
                prefixes.append((assignment.name, self.word_value(bash_syntax.Word("", assignment.value))))
        if index == len(words):
            return certain(directory)
        return self.read_command_words(values[index:], directory, prefixes, words[index + 1 :])

    def read_prefixes(self, prefixes: list[tuple[str, Word]], git: bool) -> None:
        """The assignments before a command, which its environment holds while it runs: before git, only those
        GIT_PREFIXES names with a value it allows, and those Bash reads for nothing the guard reads, since git takes
        where it acts and its configuration from its environment; before any other command, a name git does not read
        and Bash reads for nothing the guard reads."""
        for name, value in prefixes:
            if git and name in GIT_PREFIXES:
                allowed = GIT_PREFIXES[name]
                if value is None or (allowed and value not in allowed):
                    self.reading.cannot_read(f"{NOT_READ['construct assignment before git']} ({name})")
            elif git and name not in BASH_VARIABLES:
                self.reading.cannot_read(f"{NOT_READ['construct assignment before git']} ({name})")
            elif not harmless_name(name):
                self.reading.cannot_read(f"assigns {name} before a command, which Bash or git reads")

    def read_command_words(
        self,
        values: list[Word],
        directory: Directory,
        prefixes: list[tuple[str, Word]] | None = None,
        arguments_written: list[bash_syntax.Word] | None = None,
    ) -> Outcome:
        """A command's words, its name first; arguments_written are its arguments as the text writes them, from which
        a program that writes files is read with each pattern's folder (written_value)."""
        name, arguments = values[0], values[1:]
        prefixes = list(prefixes or [])
        if name is None:
            self.reading.cannot_read(
                "a command name the text does not settle, which may be any command, git among them"
            )
            return certain(UNKNOWN)
        if "/" not in name and "\\" not in name:
            if folded(name) in self.start.functions:
                self.reading.cannot_read(f"{name} is a function the environment hands the shell, which may run git")
                return certain(UNKNOWN)
            if f"builtin {name}" in NOT_READ:
                self.reading.cannot_read(f"{name}: {NOT_READ[f'builtin {name}']}")
                return certain(UNKNOWN)
            # The grammar reads [[ and coproc as a command's name; Bash reads each as a reserved word.
            if f"keyword {name}" in NOT_READ:
                self.reading.cannot_read(f"{name}: {NOT_READ[f'keyword {name}']}")
                return certain(UNKNOWN)
            if name in BUILTINS_UNDERSTOOD:
                self.read_prefixes(prefixes, git=False)
                return self.read_builtin(name, arguments, directory)
        called = command_name(name) or ""
        program, rest = multicall(built_name(called), arguments)
        if program is None:
            self.reading.cannot_read(f"{called} runs a utility the text does not settle, which may be any command")
            return certain(UNKNOWN)
        if arguments_written is not None:
            arguments_written = arguments_written[len(arguments) - len(rest) :]
        # A build a package manager installs under a prefix, as gsort, is never the platform's own (system_program).
        windows_build = False if program != called else None
        return self.read_program(program, rest, directory, prefixes, arguments_written, windows_build)

    def read_program(
        self,
        program: str,
        arguments: list[Word],
        directory: Directory,
        prefixes: list[tuple[str, Word]],
        arguments_written: list[bash_syntax.Word] | None,
        windows_build: bool | None,
    ) -> Outcome:
        """A program a command starts, by the name a table keeps for it (built_name): a runner, git, gh, a program whose
        options the guard reads, one it does not read, or one that writes the files its words name; any other is
        understood."""
        if program in RUNNERS:
            # A runner is transparent: its assignments reach the command it starts, judged against that command.
            return self.read_runner(program, arguments, directory, prefixes)
        self.read_prefixes(prefixes, git=program in GIT_NAMES)
        if program in GIT_NAMES:
            read_git(arguments, directory.cwd, self.reading)
        elif program == "gh":
            read_gh(arguments, directory, self.reading)
        elif program in PROGRAM_OPTIONS:
            variables = {name for name, _ in prefixes} | set(self.reading.environment)
            reason = program_refusal(program, arguments, variables, windows_build)
            if reason is not None:
                self.reading.cannot_read(reason)
                return certain(UNKNOWN)
            for value in program_writes(program, arguments):
                read_written(value, directory, self.reading, program_writer(program))
        elif program not in READ_ONLY_PROGRAMS:
            written = (
                [(value, False) for value in arguments]
                if arguments_written is None
                else [self.written_value(word) for word in arguments_written]
            )
            if writer_name(program) is not None:
                read_program_writes(program, written, directory, self.reading)
            else:
                read_unlisted(program, written, directory, self.reading, [name for name, _ in prefixes])
        return certain(directory)

    def read_runner(
        self, program: str, arguments: list[Word], directory: Directory, prefixes: list[tuple[str, Word]]
    ) -> Outcome:
        """A program that runs another command: refused where that command is or may be git (runner_refusal), and,
        for bash -c and pwsh -Command, where the literal script it runs holds a git call; around any other command it
        is understood, its assignments judged as ones before that command."""
        # env's own assignments are judged with the runner, by the reading both shells share (read_runner_words).
        self.read_prefixes(prefixes, git=False)
        return read_runner_words(
            program,
            arguments,
            directory,
            self.reading,
            lambda: self.script_refusal(program, arguments, directory),
            [name for name, _ in prefixes],
        )

    def script_refusal(self, program: str, arguments: list[Word], directory: Directory) -> str | None:
        """Why a shell's script is refused: a form the guard does not read, or a literal script that runs git, read
        whole as its shell reads it, each part it does not read refused as in any command."""
        script = shell_script(program, arguments)
        if script is None:
            return RUNNERS[program]
        before = len(self.reading.calls)
        if program == "bash":
            # The script runs in a shell of its own, so what it changes dies with it.
            self.nested().read_text(script, directory)
        else:
            read_powershell(script, directory.cwd, self.reading, self.depth + 1)
        if len(self.reading.calls) > before:
            return "its script runs git, which a runner's command may not be while a hub is guarded"
        return None

    def read_builtin(self, name: str, arguments: list[Word], directory: Directory) -> Outcome:
        if name == "cd":
            return self.read_cd(arguments, directory, push=False)
        if name == "pushd":
            return self.read_cd(arguments, directory, push=True)
        if name == "popd":
            if arguments:
                return certain(Directory(None, directory.stack[1:]))
            if not directory.stack:
                return certain(directory)
            return certain(Directory(directory.stack[0], directory.stack[1:]))
        if name == "printf" and arguments and (arguments[0] is None or arguments[0].startswith("-v")):
            self.reading.cannot_read("printf -v assigns a variable")
        elif name in {"test", "["}:
            self.read_test(arguments)
        elif name == "wait" and any(word is None or word.startswith("-") for word in arguments):
            self.reading.cannot_read("wait is given an option, as -p, which assigns a variable")
        elif name == "jobs" and any(word is None or (word.startswith("-") and "x" in word[1:]) for word in arguments):
            self.reading.cannot_read("jobs -x runs the command its words name, which may be git")
        elif name == "command" and (not arguments or arguments[0] not in {"-v", "-V"}):
            self.reading.cannot_read("command runs the name it is given, which may be git under another name")
        elif name == "read":
            self.read_read(arguments)
        elif name == "set":
            self.read_set(arguments)
        elif name == "shopt":
            self.read_shopt(arguments)
        return certain(directory)

    def read_cd(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """cd or pushd to a directory: a literal one settles where the shell goes; one the text does not settle, cd -,
        a CDPATH search, and pushd's rotations leave it unknown, and an option is not read."""
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        elif arguments and arguments[0] is not None and arguments[0].startswith(("-", "+")) and arguments[0] != "-":
            self.reading.cannot_read(
                f"{'pushd' if push else 'cd'}'s option {arguments[0]}, which the guard does not read"
            )
            return certain(UNKNOWN)
        stack = (directory.cwd, *directory.stack) if push else directory.stack
        if not arguments:
            if push:
                return certain(Directory(None, (None, *directory.stack[1:])))
            if self.start.home is None:
                return certain(Directory(None, stack))
            return moved_to(directory, self.start.home, stack)
        if len(arguments) > 1:
            return certain(directory)  # Bash refuses more than one directory, and stays
        target = arguments[0]
        if target is None or target == "-":
            return certain(Directory(None, stack))
        path = Path(platform_support.from_shell_path(target))
        if not path.is_absolute():
            relative_to_cwd = target.startswith(("./", "../")) or target in {".", ".."}
            if directory.cwd is None or (self.start.cdpath and not relative_to_cwd):
                return certain(Directory(None, stack))
            path = directory.cwd / path
        return moved_to(directory, path, stack)

    def read_test(self, arguments: list[Word]) -> None:
        """test and [ evaluate a subscript as arithmetic for -v, which runs what the subscript holds: refused where
        a word that is -v, or may be, has an operand after it that holds a subscript or may."""
        words = arguments[:-1] if arguments[-1:] == ["]"] else arguments
        for index, word in enumerate(words[:-1]):
            operand = words[index + 1]
            if (word is None or word == "-v") and (operand is None or "[" in operand):
                self.reading.cannot_read("test -v evaluates its operand's subscript as arithmetic")
                return

    def read_read(self, arguments: list[Word]) -> None:
        """read's options and the names it assigns, each of which must be free."""
        names: list[Word] = []
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None:
                self.reading.cannot_read("read is given a word the text does not settle, which may be any name")
                return
            if word == "--":
                names.extend(arguments[index + 1 :])
                break
            if not word.startswith("-") or len(word) == 1:
                names.extend(arguments[index:])
                break
            for position, letter in enumerate(word[1:], start=1):
                if letter in "rse":
                    continue
                if letter in "adpntNui":
                    attached = word[position + 1 :]
                    value = attached or (arguments[index + 1] if index + 1 < len(arguments) else None)
                    if letter == "a":
                        names.append(value)
                    if not attached:
                        index += 1
                    break
                self.reading.cannot_read(f"read's option -{letter}, which the guard does not read")
                return
            index += 1
        for name in names:
            if name is None or not free_name(name, self.reading.environment):
                self.reading.cannot_read(f"read assigns {name}, which Bash or git reads or the environment holds")

    def read_set(self, arguments: list[Word]) -> None:
        """set's options, each judged by SET_OPTIONS, before its positional parameters."""
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None:
                self.reading.cannot_read("set is given a word the text does not settle, which may be any option")
                return
            if word in {"--", "-"} or not word.startswith(("-", "+")):
                return
            on = word.startswith("-")
            for letter in word[1:]:
                if letter == "o":
                    index += 1
                    option = arguments[index] if index < len(arguments) else ""
                    if option != "":
                        judge_option(self.reading, "set", option, on, "set")
                        self.note_xtrace(option, on)
                else:
                    option = SET_LETTERS.get(letter)
                    judge_option(self.reading, "set", option or letter, on, "set")
                    self.note_xtrace(option, on)
            index += 1

    def note_xtrace(self, option: str | None, on: bool) -> None:
        judge_xtrace(self.reading, option, on, self.start.expanding_ps4)

    def read_shopt(self, arguments: list[Word]) -> None:
        """shopt's -s and -u, each judged by SHOPT_OPTIONS, or by SET_OPTIONS with -o; printing changes nothing."""
        state: bool | None = None
        table = "shopt"
        for word in arguments:
            if word is None:
                self.reading.cannot_read("shopt is given a word the text does not settle, which may be any option")
                return
            if word.startswith("-") and len(word) > 1:
                for letter in word[1:]:
                    if letter in "su":
                        state = letter == "s"
                    elif letter == "o":
                        table = "set"
                    elif letter not in "pq":
                        self.reading.cannot_read(f"shopt's option -{letter}, which the guard does not read")
                        return
            elif state is not None:
                judge_option(self.reading, table, word, state, "shopt")
                self.note_xtrace(word if table == "set" else None, state)


def named_paths(value: str) -> list[str]:
    """The paths a settled word may name: its value after its last =, and each quoted part of it, as a string eval
    is handed holds them, that holds a separator."""
    candidates = [value.rpartition("=")[2], *QUOTED_PART.findall(value)]
    return [candidate for candidate in candidates if "/" in candidate or "\\" in candidate]


QUOTED_PART = re.compile(r"""["']([^"']+)["']""")


def runner_refusal(program: str, arguments: list[Word]) -> str | None:
    """Why a runner is refused: one RUNNERS_ALWAYS_REFUSED names; an option RUNNER_OPTIONS does not know to be safe,
    in any spelling (option_refusal); or a word that is or may be git: one that names git or another runner, or one
    the text does not settle, which may be either. None for a runner around any other command, and for bash and pwsh,
    whose scripts script_refusal reads. A word names a runner by its built_name, as gtimeout names timeout, and a
    multicall program counts as one, since the utility it runs may be one (MULTICALL_PROGRAMS)."""
    if program in RUNNERS_ALWAYS_REFUSED:
        return RUNNERS[program]
    if program in RUNNER_OPTIONS:
        table = RUNNER_OPTIONS[program]
        reading = option_refusal(table, arguments)
        if reading.refused is not None:
            return f"{RUNNERS[program]}, and {reading.refused} is not one of its options the guard knows to be safe"
        command = next_word(arguments, reading.stop)
        variable, assigns, _ = (command or "").partition("=")
        if assigns and not table.assignments and bash_syntax.NAME.fullmatch(variable):
            # sudo, among others, sets a variable a NAME=VALUE word before its command names.
            return (
                f"{RUNNERS[program]}, and {command} may set a variable for its command, which the guard does not read"
            )
    for word in arguments:
        if word is None:
            return f"{RUNNERS[program]}, and a word the text does not settle may be git"
        name = built_name(command_name(word) or "")
        if program not in {"bash", "pwsh"} and name in GIT_NAMES | RUNNERS.keys() | MULTICALL_PROGRAMS:
            return f"{RUNNERS[program]}, and {word} names git or another runner, which may run git"
    return None


def read_runner_words(
    program: str,
    arguments: list[Word],
    directory: Directory,
    reading: Reading,
    script: Callable[[], str | None],
    variables: Iterable[str] = (),
    native: bool = False,
) -> Outcome:
    """A runner, read the same from Bash and PowerShell: refused where runner_refusal or, for bash and pwsh, script
    refuses it, or where the command it starts is a program PROGRAM_OPTIONS names that runner_command_refusal refuses;
    otherwise understood, with gh read where the runner starts it, since gh runs git itself for the subcommands
    GH_CHECKOUTS names. variables are the names assigned before the runner; env's own assignments
    (runner_assignments) and the environment's are added here, for both shells."""
    reason = runner_refusal(program, arguments)
    own = [name for name, _ in runner_assignments(program, arguments)] if reason is None else []
    if reason is None:
        # A variable a program reads options from reaches whatever the runner starts, a shell's script and a runner
        # inside it among them, whose reading does not carry the assignment, so it is refused before every runner.
        handed = sorted(PROGRAM_VARIABLE_NAMES & {name.upper() for name in (*variables, *own)})
        if handed:
            reason = f"{handed[0]} is assigned before {program}, which hands it to a program that reads options from it"
    if reason is None and program in {"bash", "pwsh"}:
        reason = script()
    if reason is None:
        # env's own assignments are ones before the command it runs, as those before env are.
        reason = next(
            (
                f"{program} assigns {name} before a command, which Bash or git reads"
                for name in own
                if not harmless_name(name)
            ),
            None,
        )
    if reason is None:
        reason = runner_command_refusal(program, arguments, {*variables, *own, *reading.environment})
    if reason is not None:
        reading.cannot_read(f"runs {program}: {reason}")
        return certain(UNKNOWN)
    if program in RUNNER_OPTIONS:
        # The runner's command is where its option reading stopped, so an option's value is never taken for gh.
        stop = option_refusal(RUNNER_OPTIONS[program], arguments).stop
        if not read_runner_command(program, arguments, stop, directory, reading, variables, native):
            return certain(UNKNOWN)
        called = command_name(arguments[stop] or "") if stop < len(arguments) else None
        start = stop if called is not None and built_name(called) == "gh" else None
    else:
        start = next(
            (index for index, word in enumerate(arguments) if word is not None and command_name(word) == "gh"), None
        )
    if start is not None:
        # chroot runs its command at the root of another tree, which the text does not place.
        read_gh(arguments[start + 1 :], UNKNOWN if program == "chroot" else directory, reading)
    return certain(directory)


def read_runner_command(
    program: str,
    arguments: list[Word],
    stop: int,
    directory: Directory,
    reading: Reading,
    variables: Iterable[str],
    native: bool,
) -> bool:
    """The command a runner RUNNER_OPTIONS names starts, at its word stop, where its option reading ended: the files a
    program PROGRAM_OPTIONS names writes, a writing program's words, and those of a program read_unlisted reads, each
    judged as they are without the runner, and gh read where xargs, which adds words from its input, does not start
    it. False where the command is a part the guard does not read, which the reading is then told."""
    called = command_name(arguments[stop] or "") if stop < len(arguments) else None
    command = built_name(called) if called is not None else None
    # chroot runs its command inside another tree, where no path the text names is the one it writes; pwsh hands a
    # native program its words as native_written reads them.
    read = native_written if native else (lambda word: (word, False))
    if command in PROGRAM_OPTIONS:
        # Its options passed its allow-list (runner_command_refusal); the files they name are judged.
        for value in program_writes(command, arguments[stop + 1 :]):
            target, pattern = (None, False) if program == "chroot" else read(value)
            read_written(target, directory, reading, program_writer(command), pattern)
    if command is None or command in {*PROGRAM_OPTIONS, *READ_ONLY_PROGRAMS}:
        return True
    if command == "gh" and program in INPUT_RUNNERS:
        # Words from its input follow gh's own, and may make any subcommand of it one that checks a branch out.
        reading.cannot_read(f"runs {program}: {program} runs gh with words from its input, which may check out")
        return False
    if command == "gh":
        return True
    if program in INPUT_RUNNERS:
        # Words from its input follow the program's own, and may name any file, a hub's among them.
        reading.cannot_read(f"runs {program}: {program} hands {command} words from its input, which may name a file")
        return False
    written = [(None, False) if program == "chroot" else read(word) for word in arguments[stop + 1 :]]
    writer = writer_name(command)
    if writer is not None:
        read_program_writes(writer, written, directory, reading)
    else:
        assigned = [*variables, *(name for name, _ in runner_assignments(program, arguments))]
        read_unlisted(command, written, directory, reading, assigned)
    return True


def system_program(program: str) -> bool:
    """Whether a program PROGRAM_OPTIONS names is judged as its Windows build too, which reads slash options: where the
    platform's own programs take them (platform_support), whatever path or name the command spells, since no spelling
    settles which build runs, and for a program whose table holds a Windows build's slash options."""
    table = PROGRAM_OPTIONS.get(program)
    return platform_support.SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS and table is not None and table.slash is not None


def runner_assignments(program: str, arguments: list[Word]) -> list[tuple[str, str]]:
    """The NAME=VALUE words a runner reads among its options as assignments for its command, as env does, read as
    option_refusal reads its words, so a value-taking option's value never ends them; none for any other runner."""
    if program not in RUNNER_OPTIONS:
        return []
    return option_refusal(RUNNER_OPTIONS[program], arguments).assigned


def runner_command_refusal(program: str, arguments: list[Word], variables: Iterable[str]) -> str | None:
    """Why the command a runner starts is refused where it is a program PROGRAM_OPTIONS names: judged as that program
    is (program_refusal), and refused after xargs, which adds words from its input, which may be any options, unless
    the program's words hold `--`, after which they are its operands."""
    if program not in RUNNER_OPTIONS:
        return None
    start = option_refusal(RUNNER_OPTIONS[program], arguments).stop
    called = command_name(arguments[start] or "") if start < len(arguments) else None
    command = built_name(called) if called is not None else None
    if command not in PROGRAM_OPTIONS:
        return None
    rest = arguments[start + 1 :]
    if program in INPUT_RUNNERS:
        # After a `--` that ends the program's options every word xargs adds is an operand; find has no such `--`, and
        # a Windows build, which reads a slash option after `--` too, none.
        stop = option_refusal(PROGRAM_OPTIONS[command], rest).stop
        table = PROGRAM_OPTIONS[command]
        if stop >= len(rest) or rest[stop] != "--" or table.single_dash or system_program(command):
            return f"{program} runs {command} with words from its input, which may be options that run a command"
    # A build a package manager installs under a prefix, as gsort, is never the platform's own (system_program).
    return program_refusal(command, rest, variables, False if command != called else None)


def program_refusal(
    program: str, arguments: list[Word], variables: Iterable[str] = (), windows_build: bool | None = None
) -> str | None:
    """Why a program PROGRAM_OPTIONS names is refused, read the same from Bash and PowerShell: an option it does not
    know to be safe, in any spelling (option_refusal), a word the text does not settle, which may be one, or a variable
    the program reads options from (PROGRAM_VARIABLES) among the names variables gives, those assigned before it and
    those of the environment. windows_build says the program is read as its Windows build too, as System32's
    sort.exe, which reads slash options, each word that starts with a slash refused unless it is exactly a safe one
    (option_refusal, slash_refusal); None leaves it to system_program."""
    if windows_build is None:
        windows_build = system_program(program)
    # Windows reads a variable's name in any case.
    handed = sorted(PROGRAM_VARIABLES.get(program, frozenset()) & {name.upper() for name in variables})
    if handed:
        return f"{program} reads options or its archive from {handed[0]}, which may hold one that runs a command"
    if any(word is None for word in arguments):
        return f"{program} may run a command its words name (a word the text does not settle)"
    table = PROGRAM_OPTIONS[program]
    reading = option_refusal(table, arguments, windows_build)
    refused = slash_refusal(table, arguments, windows_build) or reading.refused
    if refused is not None:
        return f"{program} is given {refused}, not one of its options the guard knows to be safe"
    return remote_archive(program, arguments, reading)


def remote_archive(program: str, arguments: list[Word], reading: OptionReading) -> str | None:
    """Why tar is refused for an archive it reaches through a remote shell: one named host:path, with a colon before
    any slash, which tar opens by running rsh or ssh, unless --force-local, read as an option rather than another
    option's value or a word after `--`, makes it a local file."""
    if program != "tar" or "--force-local" in reading.longs:
        return None
    for option, value in reading.values:
        if option in {"-f", "--file"} and value is not None and ":" in value.partition("/")[0]:
            return f"tar opens {value} through a remote shell, which runs a command; --force-local makes it a file"
    return None


# tar's modes that write the archive its -f or --file names: create, append, update, and concatenate, by their letters
# and long names, and --delete, which has no letter.
TAR_WRITING_LETTERS = "cruA"
TAR_WRITING_NAMES = frozenset({"--create", "--append", "--update", "--catenate", "--concatenate", "--delete"})


def program_writes(program: str, arguments: list[Word]) -> list[Word]:
    """The files a program PROGRAM_OPTIONS names writes at a path its words name, its options having passed its
    allow-list (program_refusal): the archive tar's -f or --file names, but `-` for standard output, where tar is
    given a mode that writes it (TAR_WRITING_LETTERS, TAR_WRITING_NAMES), a letter read in a bundle, or in the old
    style first word, before any letter that takes a value; none for any other program, whose allowed options write
    nothing."""
    if program != "tar":
        return []
    table = PROGRAM_OPTIONS["tar"]
    reading = option_refusal(table, arguments)
    writing = any(name in TAR_WRITING_NAMES for name in reading.longs)
    for index, word in enumerate(arguments):
        if word is None or word == "--":
            break
        old_style = index == 0 and not word.startswith("-")
        if not (old_style or (word.startswith("-") and not word.startswith("--"))):
            continue
        for letter in word if old_style else word[1:]:
            if letter in TAR_WRITING_LETTERS:
                writing = True
            if table.short.get(letter, NO_VALUE) != NO_VALUE and not old_style:
                break
    if not writing:
        return []
    return [value for option, value in reading.values if option in {"-f", "--file"} and value != "-"]


def slash_refusal(table: OptionTable, arguments: list[Word], windows_build: bool) -> str | None:
    """Where windows_build says the program is its Windows build, which reads slash options, the first word it may read
    as one that is not exactly one of the table's slash options, in any case: every word that starts with a slash,
    wherever it stands, since how such a build reads one is not documented. A joined pair such as `/r/o`, a value
    attached as in `/Oout.txt` or `/O:out.txt`, a path such as `/tmp/x`, and a word another option would take as its
    value, as `/O` after `/L` or after GNU sort's `-t`, may each be read as an option that writes, so each is refused;
    None where every such word is a safe option, and for a program with no slash options. For a table whose
    dash_switches says the build may read a dash as a slash, a word that starts with a dash is held to the same options
    as though it started with a slash, after the reading of its GNU build has judged it too."""
    if not windows_build or table.slash is None:
        return None
    prefixes = ("/", "-") if table.dash_switches else ("/",)
    return next(
        (
            word
            for word in arguments
            if word and word.startswith(prefixes) and slash_kind(table, "/" + word[1:]) is None
        ),
        None,
    )


def shell_script(program: str, arguments: list[Word]) -> str | None:
    """The literal script a shell runs, where the guard reads the shell's form: `bash -c <script>`, with any words
    after it its positional parameters, and pwsh's `-Command` or `-c` with one script after the options PWSH_QUIET
    names; None for every other form, `sh -c` among them, since sh runs in POSIX mode (RUNNERS)."""
    if program == "bash":
        if len(arguments) >= 2 and arguments[0] == "-c" and arguments[1] is not None:
            return arguments[1]
        return None
    if program != "pwsh":
        return None
    index = 0
    while index < len(arguments) and arguments[index] is not None and str(arguments[index]).lower() in PWSH_QUIET:
        index += 1
    rest = arguments[index:]
    if len(rest) == 2 and rest[0] is not None and rest[0].lower() in {"-c", "-command"} and rest[1] is not None:
        return rest[1]
    return None


REDIRECTION = ("a redirection", "The command redirects its output into")


def read_written(
    value: Word,
    directory: Directory,
    reading: Reading,
    writer: tuple[str, str] = REDIRECTION,
    pattern: bool = False,
) -> None:
    """A file a redirection, a program, or a cmdlet writes, writer naming what writes it in a part the guard does not
    read and in a refusal: one the text places in a guarded hub is refused, as a file tool's edit is
    (Judge.write_refusal), and so is one whose path or directory the text does not settle, which may lie in one. For a
    pattern, value is the folder every file it matches lies in (pattern_folder)."""
    named, opening = writer
    if value is None:
        reading.cannot_read(f"{named} writes a file the text does not settle, which may lie in a hub")
        return
    if value in DEVICE_FILES:
        return
    path = Path(platform_support.from_shell_path(value))
    if not path.is_absolute() and directory.cwd is None:
        reading.cannot_read(f"{named} writes {value} in a directory the text does not settle")
    else:
        reading.writes.append(Write(path if path.is_absolute() else (directory.cwd or Path()) / path, opening, pattern))


def pattern_folder(prefix: str) -> str:
    """The folder every file a pattern matches lies in, given the text before its first wildcard: that text up to its
    last separator, which a wildcard never matches, or `.`, the directory the command runs in, where it holds none."""
    cut = max(prefix.rfind("/"), prefix.rfind("\\"))
    return "." if cut < 0 else prefix[: cut + 1]


def program_writer(program: str) -> tuple[str, str]:
    return f"`{program}`", f"`{program}` writes"


def written_words(program: str, words: Sequence[tuple[Word, bool]]) -> list[tuple[Word, bool]]:
    """The words of a WRITING_PROGRAMS program that may name a file it writes, each with whether it is the folder of a
    pattern: every operand, a word after `--` among them; the value of a long option after its `=`; and in a bundle
    of letters, the rest of the word after its first letter the program's flags do not name, which may be that
    option's value, the next word, when it takes one, being an operand anyway. dd writes only at its `of=` operand,
    and sed only in place (sed_in_place). A word the text does not settle may be any of these."""
    if program == "dd":
        return [
            (None if pattern or word is None else word.partition("=")[2], False)
            for word, pattern in words
            if pattern or word is None or word.partition("=")[0] == "of"
        ]
    if program == "sed" and not sed_in_place(words):
        return []
    flags = WRITING_PROGRAMS[program]
    found: list[tuple[Word, bool]] = []
    operands = False
    for word, pattern in words:
        if pattern or word is None or operands or not word.startswith("-"):
            if pattern or word != "":
                found.append((word, pattern))
        elif word == "--":
            operands = True
        elif word.startswith("--"):
            _, assigned, value = word.partition("=")
            if assigned and value:
                found.append((value, False))
        elif flags is not None:
            position = next((index for index, letter in enumerate(word[1:], 1) if letter not in flags), None)
            if position is not None and word[position + 1 :]:
                found.append((word[position + 1 :], False))
    return found


def sed_in_place(words: Sequence[tuple[Word, bool]]) -> bool:
    """Whether sed rewrites the files it is given (SED_IN_PLACE): an option before `--` that is -i or -I, alone or
    in a bundle before any letter that takes a value, a letter neither a flag nor one of those, a long option that
    is a prefix of --in-place, or a word the text does not settle, which may be any of these."""
    letters, valued, long_name = SED_IN_PLACE
    flags = WRITING_PROGRAMS["sed"] or ""
    for word, pattern in words:
        if pattern:
            continue
        if word is None:
            return True
        if word == "--":
            return False
        if word.startswith("--"):
            name = word[2:].partition("=")[0]
            if name and long_name.startswith(name):
                return True
        elif word.startswith("-"):
            for letter in word[1:]:
                if letter in letters or (letter not in flags and letter not in valued):
                    return True
                if letter in valued:
                    break
    return False


def makes_link(program: str, words: Sequence[tuple[Word, bool]]) -> bool:
    """Whether a WRITING_PROGRAMS program is given an option LINK_OPTIONS names, before `--`: a letter of a bundle
    before any letter that takes a value, or a long option that is a prefix of one of its names."""
    if program not in LINK_OPTIONS:
        return False
    letters, names = LINK_OPTIONS[program]
    flags = WRITING_PROGRAMS[program] or ""
    for word, pattern in words:
        if pattern or word is None or not word.startswith("-"):
            continue
        if word == "--":
            return False
        if word.startswith("--"):
            name = word[2:].partition("=")[0]
            if name and any(each.startswith(name) for each in names):
                return True
            continue
        for letter in word[1:]:
            if letter in letters:
                return True
            if letter not in flags:
                break
    return False


def read_program_writes(
    program: str, words: Sequence[tuple[Word, bool]], directory: Directory, reading: Reading
) -> None:
    """A program WRITING_PROGRAMS or LINK_PROGRAMS names, read the same from Bash and PowerShell, its words each with
    whether it is the folder of a pattern: one that makes a link is a part the guard does not read, since a write
    through the link later in the command reaches a place the guard placed before it existed; every other word that
    may name a file it writes (written_words) is judged as a redirection's target is."""
    if program in LINK_PROGRAMS:
        reading.cannot_read(
            f"{program} {LINK_PROGRAMS[program]}, through which a later write reaches a place the guard cannot follow"
        )
        return
    if makes_link(program, words):
        reading.cannot_read(
            f"{program} is given an option that makes a link, through which a later write reaches a place the guard "
            "cannot follow"
        )
        return
    if program == "sed":
        reason, scripted = sed_writes(words)
        if reason is not None:
            reading.cannot_read(reason)
            return
        for name in scripted:
            read_written(name, directory, reading, program_writer(program))
    for value, pattern in written_words(program, words):
        read_written(value, directory, reading, program_writer(program), pattern)


def read_unlisted(
    program: str,
    words: Sequence[tuple[Word, bool]],
    directory: Directory,
    reading: Reading,
    assigned: Iterable[str] = (),
) -> None:
    """A program no table of the guard reads and READ_ONLY_PROGRAMS does not name, read the same from Bash and
    PowerShell and where a runner starts it, its words each with whether it is the folder of a pattern: a word the text
    does not settle, which may name any file, and one that names git or a runner, which it may run, are parts the
    guard does not read; each path a word may name (word_paths) is judged as a redirection's target is, and so is a
    pattern's folder. Python running a script is read the same, but its words from the script on are recorded with
    the script (ScriptRun) and count only where a guarded hub does not track it, while the interpreter's own options
    before it, the values of -X and -W among them, are judged as any; assigned names the variables assigned before
    it, and one Python reads, as PYTHONPATH, which chooses the modules the script imports, leaves no script trusted."""
    found = python_script(program, words, directory)
    if any(name.upper().startswith("PYTHON") for name in assigned):
        found = None
    start = len(words) if found is None else found[1]
    judge_words(program, words[:start], directory, reading)
    if found is not None:
        own = Reading(reading.environment)
        judge_words(program, words[start:], directory, own)
        reading.scripts.append(ScriptRun(found[0], own.writes, own.unread))


def judge_words(program: str, words: Sequence[tuple[Word, bool]], directory: Directory, reading: Reading) -> None:
    """The words of a program read_unlisted reads, judged into reading: a word the text does not settle and one that
    names git or a runner are parts the guard does not read, and each path a word may name, or a pattern's folder, is
    judged as a redirection's target is."""
    opening = f"`{program}` is given"
    for word, pattern in words:
        if word is None:
            reading.cannot_read(f"`{program}` is given a word the text does not settle, which may name a file in a hub")
            continue
        for value in (word, word.rpartition("=")[2]):
            if not pattern and built_name(command_name(value) or "") in GIT_NAMES | RUNNERS.keys() | MULTICALL_PROGRAMS:
                reading.cannot_read(f"`{program}` is given {word}, which names git or a runner, which it may run")
        for path in [word] if pattern else word_paths(word, directory, reading.environment):
            if path in DEVICE_FILES:
                continue
            if directory.cwd is None and not Path(platform_support.from_shell_path(path)).is_absolute():
                reading.cannot_read(f"`{program}` is given {path} in a directory the text does not settle")
            else:
                reading.writes.append(Write(resolved(path, directory), opening, pattern))


def resolved(value: str, directory: Directory) -> Path:
    """A path a word names, read from the directory the command runs in where it is relative."""
    path = Path(platform_support.from_shell_path(value))
    return path if path.is_absolute() else (directory.cwd or Path()) / path


def word_paths(word: str, directory: Directory, environment: Mapping[str, str]) -> list[str]:
    """The paths a settled word of a program read_unlisted reads may name: the word itself, but for an option; the
    rest of a bundle of one dash after each of its letters, which may be an option's value; its value after its first
    and its last `=`; each quoted part of it and each of its blank-separated parts, as a script or a command line it is
    handed holds them. Of these, a value counts as a path only where it may be one: an absolute path, one that starts
    with `~`, read from the home, `.`, `..`, `./`, or `../`, or a relative one whose first name is there in the
    directory the command runs in, or any relative one where the text does not settle that directory. A relative word
    whose first name does not exist yet, as out.txt or out/report.json, is not one, so a program may make new files and
    folders under a new name at the top of the directory it runs in."""
    candidates = [] if word.startswith("-") else [word]
    if word.startswith("-") and not word.startswith("--"):
        # The rest after each letter of the bundle, up to the first character that is no option letter.
        index = 2
        while index < len(word) and word[index - 1].isalnum():
            candidates.append(word[index:])
            index += 1
    if "=" in word:
        candidates.extend((word.partition("=")[2], word.rpartition("=")[2]))
    candidates.extend(QUOTED_PART.findall(word))
    candidates.extend(part for part in word.split() if not part.startswith("-"))
    found: list[str] = []
    for candidate in dict.fromkeys(candidates):
        path = path_value(candidate, directory, environment)
        if path is not None and path not in found:
            found.append(path)
    return found


def path_value(candidate: str, directory: Directory, environment: Mapping[str, str]) -> str | None:
    """The path a value names where it may name one (word_paths), with a leading `~` read from the home; None where it
    may not. A relative value whose first name the guard cannot look up counts as one, but for a first name holding a
    character no Windows file name holds (NO_FILE_NAME), which names no file."""
    if not candidate:
        return None
    if candidate == "~" or candidate.startswith(("~/", "~\\")):
        start = home(environment)
        return None if start is None else str(start) + candidate[1:]
    path = Path(platform_support.from_shell_path(candidate))
    # A drive-relative path, as C:x, names a file in that drive's current directory, which may be a hub.
    if path.is_absolute() or path.drive:
        return candidate
    if candidate in {".", ".."} or candidate.startswith(("./", "../", ".\\", "..\\")):
        return candidate
    if directory.cwd is None:
        return candidate
    first = re.split(r"[/\\]", candidate, maxsplit=1)[0]
    # A first name holding a character no Windows file name holds names no file there, where a look-up of it fails
    # rather than finding nothing, so on every platform it is no path, as `https:` or a script's `open("x")` is.
    if NO_FILE_NAME.search(first):
        return None
    try:
        return candidate if file_presence.is_occupied(directory.cwd / first) else None
    except OSError:
        return candidate


def python_script(program: str, words: Sequence[tuple[Word, bool]], directory: Directory) -> tuple[Path, int] | None:
    """The script Python runs, placed from the directory the command runs in, with the index of its word, where program
    is one of PYTHON_NAMES and its words before a literal operand are only the options PYTHON_FLAGS and PYTHON_VALUED
    name; None for any other program or form, as `-c`, `-m`, `-i`, or a script the text does not settle or place."""
    if not PYTHON_NAMES.fullmatch(program):
        return None
    index = 0
    while index < len(words):
        word, pattern = words[index]
        if pattern or word is None or word == "-":
            return None
        if not word.startswith("-"):
            path = Path(platform_support.from_shell_path(word))
            if path.is_absolute():
                return path, index
            return None if directory.cwd is None else (directory.cwd / path, index)
        if word.startswith("--"):
            return None
        index += 1
        for position, letter in enumerate(word[1:], 1):
            if letter in PYTHON_VALUED:
                index += position == len(word) - 1
                break
            if letter not in PYTHON_FLAGS:
                return None
    return None


def is_trusted_script(script: Path) -> bool:
    """Whether a Python script is one TRUSTED_SCRIPTS names that a guarded hub tracks, with every link on the way to it
    followed: the guard refuses every edit to such a file and every git call that writes it, so it runs what the hub's
    history holds. A hub file TRUSTED_SCRIPTS does not name, a file the hub does not track, an ignored one or a new
    one among them, and any file elsewhere is not."""
    real = Path(os.path.realpath(script))
    hub = protecting_hub(real)
    if hub is None or not file_presence.is_present(real, other_is_absent=True):
        return False
    relative = Path(os.path.relpath(real, os.path.realpath(hub))).as_posix()
    if relative not in TRUSTED_SCRIPTS:
        return False
    # A literal pathspec matches no other file a wildcard in its name would match, and the answer must name this file
    # alone, as a folder's pathspec names every file under it.
    arguments = ["ls-files", "-z", "--", f":(literal){real}"]
    answer = CLIENT.quiet_output(arguments, directory=hub, timeout=lookup_timeout()) or ""
    return answer.split("\0") == [relative, ""]


def read_names() -> frozenset[str]:
    """The program names a table of the guard keeps: the runners, the programs whose options it reads, the ones that
    write the files their words name or make a link, and the multicall programs; never READ_ONLY_PROGRAMS, whose
    names are known by themselves alone."""
    return frozenset(
        {
            *RUNNERS,
            *PROGRAM_OPTIONS,
            *WRITING_PROGRAMS,
            *LINK_PROGRAMS,
            *MULTICALL_PROGRAMS,
            *GIT_NAMES,
            "gh",
        }
    )


def built_name(program: str) -> str:
    """The name a table of the guard keeps for a program (read_names): its own; the one BUILD_NAMES names for another
    build of it, as bsdtar; or, for a build a package manager installs under a prefix BUILD_PREFIXES names, as gcp,
    gtimeout, gsort, or ucp, the name after the prefix. Any other name is returned as it is."""
    names = read_names()
    if program in names:
        return program
    if program in BUILD_NAMES:
        return BUILD_NAMES[program]
    unprefixed = program[1:]
    if program[:1] in BUILD_PREFIXES and unprefixed in names and unprefixed not in GIT_NAMES | {"gh"}:
        return unprefixed
    return program


def writer_name(program: str) -> str | None:
    """The name WRITING_PROGRAMS or LINK_PROGRAMS keeps for a program (built_name); None for any other."""
    name = built_name(program)
    return name if name in WRITING_PROGRAMS.keys() | LINK_PROGRAMS.keys() else None


def multicall(program: str, arguments: list[Word]) -> tuple[str | None, list[Word]]:
    """The utility a multicall program MULTICALL_PROGRAMS names runs, by its built_name, with its words: the one its
    first word names, or its `--coreutils-prog=<name>`, read again where it is one too; None where the text does not
    settle it, or the program is given an option before it. Any other program is returned with its words."""
    while program in MULTICALL_PROGRAMS:
        if not arguments or arguments[0] is None:
            return None, arguments
        first = arguments[0]
        if first.startswith(MULTICALL_OPTION):
            first = first.removeprefix(MULTICALL_OPTION)
        elif first.startswith("-"):
            return None, arguments
        program, arguments = built_name(command_name(first) or ""), arguments[1:]
    return program, arguments


@dataclass
class SedOptions:
    """sed's words as one build reads its options (SED_BUILDS): the scripts it runs, a script the text does not settle
    being None, whether one comes from a script file, and its operands; refused is set where the build refuses them,
    so sed runs nothing."""

    build: str
    words: Sequence[tuple[Word, bool]]
    scripts: list[Word] = field(default_factory=list)
    script_file: bool = False
    operands: list[Word] = field(default_factory=list)
    refused: bool = False
    index: int = 0

    def next_value(self) -> Word:
        """The word after an option that takes one; a pattern is not settled, and none at all refuses the options."""
        if self.index >= len(self.words):
            self.refused = True
            return None
        word, pattern = self.words[self.index]
        self.index += 1
        return None if pattern else word

    def take(self, option: str, value: Word) -> None:
        if option in {"e", "expression"}:
            self.scripts.append(value)
        self.script_file = self.script_file or option in {"f", "file"}

    def read_long(self, word: str) -> None:
        name, assigned, value = word[2:].partition("=")
        matches = (
            [name] if name in SED_LONG_OPTIONS else [option for option in SED_LONG_OPTIONS if option.startswith(name)]
        )
        if self.build != "gnu" or not name or len(matches) != 1:
            self.refused = True
            return
        if SED_LONG_OPTIONS[matches[0]]:
            self.take(matches[0], value if assigned else self.next_value())

    def read_bundle(self, word: str) -> None:
        flags, valued, optional = SED_BUILDS[self.build]
        for position, letter in enumerate(word[1:], 1):
            if letter in optional:
                return
            if letter in valued:
                self.take(letter, word[position + 1 :] or self.next_value())
                return
            if letter not in flags:
                self.refused = True
                return

    def read(self) -> SedOptions:
        ended = False
        while self.index < len(self.words) and not self.refused:
            word, pattern = self.words[self.index]
            self.index += 1
            if pattern or ended or word is None or not word.startswith("-") or word == "-":
                self.operands.append(None if pattern else word)
                # BSD sed reads no option after its first operand.
                ended = ended or self.build == "bsd"
            elif word == "--":
                ended = True
            elif word.startswith("--"):
                self.read_long(word)
            else:
                self.read_bundle(word)
        if not self.scripts and not self.script_file:
            if self.operands:
                self.scripts.append(self.operands[0])
            else:
                self.refused = True
        return self


def sed_writes(words: Sequence[tuple[Word, bool]]) -> tuple[str | None, list[str]]:
    """Why sed is a part the guard does not read, or the files its scripts write, read as each build of it reads its
    options (SedOptions) and its script as GNU sed, whose commands hold BSD sed's (SedScript): a script file, a script
    the text does not settle, and a script that runs a command are parts the guard does not read; a build that refuses
    the options or the script runs nothing, and where every build does, the guard cannot tell what sed runs."""
    written: list[str] = []
    read = False
    for build in SED_BUILDS:
        options = SedOptions(build, words).read()
        if options.refused:
            continue
        if options.script_file:
            return "sed is given a script file, which the guard does not read and which may write a file", []
        if None in options.scripts:
            return "sed is given a script the text does not settle, which may write a file or run a command", []
        scripts = [SedScript(script or "").read() for script in options.scripts]
        if any(script.refused for script in scripts):
            continue
        if any(script.runs for script in scripts):
            return "sed's script runs a command, by an e command or an s command's e flag, which may be git", []
        written.extend(name for script in scripts for name in script.written)
        read = True
    if not read:
        return "sed is given options or a script the guard cannot follow, which may write a file", []
    # Each build that reads the same script names the same files once.
    return None, list(dict.fromkeys(written))


@dataclass
class SedScript:
    """What a sed script does that the guard judges, read as GNU sed reads one: whether it runs a command, by an e
    command or an s command's e flag, and the files its w and W commands and its s commands' w flag write, each the
    rest of its line. refused is set for a script the guard cannot follow, a command it does not know among them,
    which sed refuses too."""

    text: str
    index: int = 0
    runs: bool = False
    written: list[str] = field(default_factory=list)
    refused: bool = False

    def line_end(self, start: int) -> int:
        found = self.text.find("\n", start)
        return len(self.text) if found < 0 else found

    def blanks(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t":
            self.index += 1

    def delimited(self, start: int, pattern: bool = False) -> int | None:
        """The index after the part a delimiter at start opens and the same delimiter closes, escapes skipped; None
        where it does not close on its line. In a pattern, a bracket expression is read whole (bracket_end), since BSD
        sed reads a delimiter inside one as a member and GNU sed as the pattern's end."""
        text = self.text
        if start >= len(text) or text[start] in "\n\\":
            return None
        delimiter, position = text[start], start + 1
        while position < len(text) and text[position] != delimiter:
            if text[position] == "\n":
                return None
            if pattern and text[position] == "[":
                closed = self.bracket_end(position, delimiter)
                if closed is None:
                    return None
                position = closed
                continue
            position += 2 if text[position] == "\\" else 1
        return position + 1 if position < len(text) else None

    def bracket_end(self, start: int, delimiter: str) -> int | None:
        """The index after a bracket expression opening at start, read as POSIX reads one: a leading ^ and then a ]
        are members, and [: :], [= =], and [. .] hold the name between them. None where it does not close on its line
        or holds the delimiter, which the builds read apart, so the guard cannot tell where the pattern ends."""
        text = self.text
        position = start + 1
        if position < len(text) and text[position] == "^":
            position += 1
        if position < len(text) and text[position] == "]":
            position += 1
        while position < len(text) and text[position] != "]":
            if text[position] in ("\n", delimiter):
                return None
            if text[position] == "[" and text[position + 1 : position + 2] in (":", "=", "."):
                close = text.find(text[position + 1] + "]", position + 2)
                if close < 0 or "\n" in text[position:close] or delimiter in text[position:close]:
                    return None
                position = close + 2
                continue
            position += 1
        return position + 1 if position < len(text) else None

    def address(self) -> bool:
        """Reads one address, a line, $, a step or an offset, or a pattern between delimiters with its flags; False
        where none stands here, refused where a pattern is not closed."""
        text = self.text
        self.blanks()
        if self.index < len(text) and (text[self.index].isdigit() or text[self.index] in "$+~"):
            self.index += 1
            while self.index < len(text) and (text[self.index].isdigit() or text[self.index] == "~"):
                self.index += 1
            return True
        if self.index < len(text) and text[self.index] in "/\\":
            closed = self.delimited(self.index + 1 if text[self.index] == "\\" else self.index, pattern=True)
            if closed is None:
                self.refused = True
                return False
            self.index = closed
            while self.index < len(text) and text[self.index] in "IM":
                self.index += 1
            return True
        return False

    def backslashes_before(self, end: int) -> int:
        """The length of the run of backslashes that ends just before end."""
        count = 0
        while end - count > 0 and self.text[end - count - 1] == "\\":
            count += 1
        return count

    def file_name(self, start: int) -> None:
        """A w command's or flag's file, the rest of the line."""
        name = self.text[start : self.line_end(start)].strip()
        if name:
            self.written.append(name)
        else:
            self.refused = True
        self.index = self.line_end(start)

    def substitute(self, command: str) -> None:
        """An s or y command's two parts, and an s command's flags."""
        first = self.delimited(self.index, pattern=command == "s")
        second = None if first is None else self.delimited(first - 1)
        if second is None:
            self.refused = True
            return
        self.index = second
        while command == "s" and self.index < len(self.text) and self.text[self.index] not in ";\n}":
            flag = self.text[self.index]
            if flag == "w":
                self.file_name(self.index + 1)
                return
            self.runs = self.runs or flag == "e"
            self.refused = self.refused or (flag != "e" and flag not in SED_SUBSTITUTE_FLAGS)
            self.index += 1

    def command(self, command: str) -> None:
        text = self.text
        if command in SED_COUNTED_COMMANDS:
            self.blanks()
            while self.index < len(text) and text[self.index].isdigit():
                self.index += 1
        elif command in "wW":
            self.file_name(self.index)
        elif command == "e" or command in SED_LINE_COMMANDS:
            self.runs = self.runs or command == "e"
            self.index = self.line_end(self.index)
        elif command in SED_LABEL_COMMANDS:
            while self.index < len(text) and text[self.index] not in ";\n":
                self.index += 1
        elif command in SED_TEXT_COMMANDS:
            # The text goes on past a line that ends in an odd run of backslashes, the last escaping the newline; an
            # even run is escaped backslashes, and the next line is a command.
            self.index = self.line_end(self.index)
            while 0 < self.index < len(text) and self.backslashes_before(self.index) % 2:
                self.index = self.line_end(self.index + 1)
        elif command in "sy":
            self.substitute(command)
        elif command not in SED_PLAIN_COMMANDS:
            self.refused = True

    def stopped(self) -> bool:
        """Whether reading has stopped at a part sed refuses; a method, since each step may set it."""
        return self.refused

    def read(self) -> SedScript:
        text = self.text
        while self.index < len(text) and not self.refused:
            if text[self.index] in " \t\n;":
                self.index += 1
                continue
            if text[self.index] == "#":
                self.index = self.line_end(self.index)
                continue
            # Up to two addresses, joined by a comma, then `!`.
            if self.address():
                self.blanks()
                if self.index < len(text) and text[self.index] == ",":
                    self.index += 1
                    self.address()
            while self.index < len(text) and text[self.index] in " \t!":
                self.index += 1
            # An address that does not close, or one with no command after it, is a script sed refuses.
            if self.index >= len(text):
                self.refused = True
            if self.stopped():
                break
            self.index += 1
            self.command(text[self.index - 1])
        return self


def provider_refusal(name: str, cmdlet: str, values: list[Word]) -> str | None:
    """Why a path cmdlet's values are not read: one that names a provider other than the file system, as env: or
    alias:, whether given by position or attached to a parameter by a colon, and, for a cmdlet that writes, one the text
    does not settle, which may name such a provider; None for any other command."""
    if cmdlet not in POWERSHELL_PATH_COMMANDS:
        return None
    for value in values:
        if value is None and cmdlet in POWERSHELL_WRITING_COMMANDS:
            return f"{name} writes at a path the text does not settle, which may lie on a provider such as env:"
        if value is not None and PROVIDER_PATH.match(value) and not value.startswith("-"):
            return f"{name} names {value}, a path on a provider other than the file system"
    return None


def powershell_parameter(word: str, cmdlet: str) -> tuple[str, str]:
    """A writing cmdlet's parameter, as PowerShell binds it (POWERSHELL_CMDLET_PARAMETERS), by its kind, "path", "data",
    or "switch", and its name: the parameter its name or an alias names, or else the one a prefix names alone among the
    cmdlet's own, or, where it names none of them, among the common ones. A prefix of several, which pwsh refuses as
    ambiguous and another release may bind, is read as the path among them, one that takes a wildcard first, or else
    as the item type, which is held to the types that make no link, and else as taking no value, so the value after
    it is read as one given by position; and so is a prefix of none, which may be a parameter a provider adds."""
    written = word.lstrip("-").rstrip(":").lower()
    own = {
        spelling: name
        for name, aliases in POWERSHELL_CMDLET_PARAMETERS.get(cmdlet, {}).items()
        for spelling in (name, *aliases)
    }
    common = {spelling: name for name, aliases in POWERSHELL_COMMON_PARAMETERS.items() for spelling in (name, *aliases)}
    if written in own or written in common:
        matches = {own.get(written) or common[written]}
    else:
        matches = {name for spelling, name in own.items() if written and spelling.startswith(written)}
        matches = matches or {name for spelling, name in common.items() if written and spelling.startswith(written)}
    paths = sorted(matches & POWERSHELL_PATH_PARAMETERS, key=lambda name: (name in POWERSHELL_LITERAL_PATHS, name))
    if paths:
        return "path", paths[0]
    if "itemtype" in matches:
        return "data", "itemtype"
    if len(matches) == 1:
        name = next(iter(matches))
        return ("data" if name in POWERSHELL_DATA_PARAMETERS | POWERSHELL_COMMON_DATA else "switch"), name
    return "switch", written


def native_written(word: Word) -> tuple[Word, bool]:
    """A native program's word from PowerShell, as a program WRITING_PROGRAMS names is read: a leading ~, which pwsh
    hands on as written or expands by release, is not settled, and a wildcard, which pwsh off Windows expands, makes
    the word a pattern, judged by the folder its matches lie in."""
    if word is None or word.startswith("~"):
        return None, False
    wildcard = POWERSHELL_WILDCARDS.search(word)
    return (word, False) if wildcard is None else (pattern_folder(word[: wildcard.start()]), True)


def powershell_command(name: str) -> bool:
    """Whether PowerShell may find a name among its own commands before it looks for a program: an alias, a function it
    defines itself, or any name holding a hyphen and no directory, as a cmdlet's or a function's verb-noun name does.
    A module PowerShell loads on demand may define a function by such a name whatever its verb, as Windows' own
    BitLocker, DeliveryOptimization, and EventTracing modules define Backup-, Delete-, and Flush- ones, and which
    modules a machine holds the text does not show. A program's name written with the extension Windows lets a caller
    add (platform_support.program_name), as dotnet-format.exe, or as a path names the program alone."""
    lowered = name.lower()
    with_extension = platform_support.program_name(lowered) != lowered
    bare = "-" in lowered and "/" not in lowered and "\\" not in lowered and not with_extension
    return lowered in POWERSHELL_ALIASES or lowered in POWERSHELL_FUNCTIONS or bare


def read_gh(arguments: list[Word], directory: Directory, reading: Reading) -> None:
    """gh, which runs git itself to check a branch out for the subcommands GH_CHECKOUTS names, in the checkout it
    runs in, and writes files into that directory, or where its words name, for those GH_WRITERS names, which are
    judged as the directory a command writes into and as a program read_unlisted reads; one GH_CONFIGURATION_WRITERS
    names writes a git configuration, so it is not read. A first word is read by its alias too (GH_ALIASES). A flag
    before the words that would name such a subcommand, other than one of GH_VALUED_FLAGS, is not read: cobra takes the
    word after a flag it does not know as that flag's value, so `gh pr -b x checkout 12` checks a branch out while its
    first two words are pr and x."""
    named: list[Word] = []
    unknown_flag: str | None = None
    words = iter(arguments)
    for word in words:
        if word in GH_VALUED_FLAGS:
            next(words, None)
        elif word is None or not word.startswith("-"):
            named.append(word)
        elif unknown_flag is None and gh_checkout_prefix(named) and not gh_flag_with_value(word):
            unknown_flag = word
    named = named[:2]
    if named[:1] != [None] and named:
        named[0] = GH_ALIASES.get(str(named[0]), named[0])
    if len(named) == 2 and None not in named:
        named = list(GH_SUBCOMMAND_ALIASES.get((str(named[0]), str(named[1])), tuple(named)))
    if named[:1] == [None] or (gh_checkout_prefix(named[:1]) and None in named):
        reading.cannot_read("a gh subcommand the text does not settle, which may check a branch out or write files")
    elif unknown_flag is not None:
        reading.cannot_read(
            f"gh's {unknown_flag} before its subcommand, whose next word gh may take as its value, so the subcommand "
            "may be one that checks a branch out or writes files"
        )
    elif tuple(named) in GH_CHECKOUTS:
        # gh runs git in its own directory with the environment it was given, so the inherited variables place it.
        values = git_variables(inherited_variables(reading.environment), reading.notes)
        place = GitOptions.initial(directory.cwd, values, opaque=False).place()
        reading.calls.append(GitCall(place, " ".join(str(word) for word in named), [], program="gh"))
    elif tuple(named) in GH_CONFIGURATION_WRITERS:
        reading.refuse(
            f"`gh {' '.join(str(word) for word in named)}` writes a git configuration, its repository's, which every "
            "worktree shares with its hub, or the global one, so it is refused while a hub is guarded, from the hub "
            "and from a worktree alike. Run it from a terminal outside the clone."
        )
    elif tuple(named) in GH_WRITERS:
        # It writes into the directory it runs in, as a clone or a download does by default, or where its words name.
        subcommand = f"`gh {' '.join(str(word) for word in named)}`"
        if directory.cwd is None:
            reading.cannot_read(f"{subcommand} writes into a directory the text does not settle")
        else:
            reading.writes.append(Write(directory.cwd, f"{subcommand} writes into", folder=True))
        read_unlisted("gh", [(word, False) for word in arguments], directory, reading)


def gh_checkout_prefix(named: Sequence[Word]) -> bool:
    """Whether the words gh has read so far may still begin a subcommand GH_CHECKOUTS, GH_WRITERS, or
    GH_CONFIGURATION_WRITERS names: none yet, or one of their first words alone, by its name or an alias
    (GH_ALIASES)."""
    first = GH_ALIASES.get(str(named[0]), named[0]) if named else None
    keys = GH_CHECKOUTS | GH_WRITERS | GH_CONFIGURATION_WRITERS
    return not named or (len(named) == 1 and any(first == key[0] for key in keys))


def gh_flag_with_value(word: str) -> bool:
    """Whether a flag word of gh carries its value as one of GH_VALUED_FLAGS does: after = for a long one, or
    attached to -R."""
    name, equals, _ = word.partition("=")
    return (bool(equals) and name in GH_VALUED_FLAGS) or (word.startswith("-R") and len(word) > 2)


NOT_READ_XTRACE = "xtrace expands PS4 before each command, and the environment's PS4 holds an expansion, which may run"
# A redirection's target that names a descriptor to duplicate or close, as in 2>&1 or >&-.
DESCRIPTOR_TARGET = re.compile(r"[0-9]+-?|-")


def expansion_form(form: str) -> str:
    """The catalogue entry of an expansion, by the form bash_syntax gives its dynamic part."""
    if form.startswith("${") and form.endswith("}"):
        return parameter_form(form[2:-1])
    if form.startswith("$") and len(form) == 2:
        return "$1" if form[1].isdigit() else form
    return "NAME=( )" if form == "(" else form


def parameter_form(content: str) -> str:
    """The catalogue entry of a braced expansion's content: a name and an operator from PARAMETER_OPERATORS, read
    only when its word holds no further expansion the guard would have to read apart."""
    if not content or content[0] in " \t\n":
        return "${ command; }"
    if content[0] == "|":
        return "${| command; }"
    if content[0] == "!":
        return "${!NAME}"
    if content[0] == "#" and len(content) > 1 and PARAMETER_NAME.fullmatch(content[1:]):
        return "${#NAME}"
    name = PARAMETER_NAME.match(content)
    if name is None:
        return "${unread}"
    rest = content[name.end() :]
    if not rest:
        return "${NAME}"
    for operator, key in PARAMETER_OPERATORS:
        if rest.startswith(operator):
            word = rest[len(operator) :]
            if key in EXPANSIONS_UNDERSTOOD and any(mark in word for mark in ("${", "$((", "$[")):
                return "nested ${"
            return key
    return "${unread}"


def arithmetic_command(node: bash_syntax.Subshell) -> bool:
    """Whether a subshell is an arithmetic command, (( )), which the grammar reads as a subshell holding only a
    subshell; ( ( ) ), which Bash runs the same, reads alike."""
    body = node.body
    if len(body.items) != 1:
        return False
    and_or, background = body.items[0]
    commands = and_or.first.commands
    return not background and not and_or.rest and len(commands) == 1 and isinstance(commands[0], bash_syntax.Subshell)


def command_bodies(node: bash_syntax.Command) -> list[bash_syntax.CommandList]:
    """The lists a compound command runs, its conditions included."""
    if isinstance(node, (bash_syntax.Subshell, bash_syntax.Group)):
        return [node.body]
    if isinstance(node, bash_syntax.If):
        lists = [each for clause in node.clauses for each in clause]
        return [*lists, node.otherwise] if node.otherwise is not None else lists
    if isinstance(node, bash_syntax.Loop):
        return [node.condition, node.body] if node.condition is not None else [node.body]
    if isinstance(node, bash_syntax.Case):
        return list(node.branches)
    return []


def changes_directory(node: object) -> bool:
    """Whether a compound command holds a command that may change the shell's directory: cd, pushd, or popd, or one
    whose name the text does not settle, which is refused while a hub is in view anyway."""
    if isinstance(node, bash_syntax.Simple):
        names = [word for word in node.words if word.assignment is None][:1]
        return bool(names) and names[0].literal in {"cd", "pushd", "popd", None}
    if isinstance(node, bash_syntax.CommandList):
        return any(changes_directory(and_or) for and_or, _ in node.items)
    if isinstance(node, bash_syntax.AndOr):
        return any(changes_directory(pipeline) for pipeline in (node.first, *(each for _, each in node.rest)))
    if isinstance(node, bash_syntax.Pipeline):
        return any(changes_directory(command) for command in node.commands)
    return isinstance(node, bash_syntax.Command) and any(changes_directory(body) for body in command_bodies(node))


def read_bash(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a Bash command started in cwd with the environment the hook was given."""
    BashReader(reading, depth, bash_start(reading)).read_text(command, Directory(cwd))


# ---------------------------------------------------------------------------------------------------------------
# The PowerShell reader


@functools.cache
def powershell_reader() -> str:
    """The reader as pwsh is handed it on its command line: without its comment lines, which run nothing and would
    otherwise bring it past the 32767 characters a command line Windows starts may hold. The reader holds no
    here-string, so no line of a string begins with #. Its lines break where PowerShell breaks them, at CR, LF, and
    CRLF alone (text_lines), and a comment line is one whose # follows only the spaces and tabs COMMENT_INDENT names:
    a line whose # follows a form feed, a file separator, or another character str.lstrip skips is kept, and so is
    one str.splitlines would end there, since keeping a comment costs only its length and dropping a line that is not
    one changes what the reader runs."""
    lines = text_lines(POWERSHELL_READER.read_text(encoding="utf-8"))
    return "\n".join(line for line in lines if not line.lstrip(COMMENT_INDENT).startswith("#"))


def powershell_events(command: str, reading: Reading) -> dict[str, Any] | None:
    """What PowerShell's own parser makes of one command, or None, with why, when it could not be read.

    pwsh starts with -Command rather than -File, so no execution policy applies, and not with -EncodedCommand,
    which endpoint protection treats as a sign of malware. The command travels base64-encoded on stdin, past any
    console code page.
    """
    pwsh = platform_support.find_pwsh(reading.environment.get("PATH", ""))
    if pwsh is None:
        reading.cannot_read("could not find pwsh to read a PowerShell command")
        return None
    try:
        # A command holding a lone surrogate has no JSON form, which json_writer refuses here.
        payload = json_writer.dumps({"command": command})
        # run_bounded takes git's repository-selecting variables out of the hook's environment, and the tree ends
        # every process pwsh starts once it has finished or been killed.
        result = run_bounded(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershell_reader()],
            process_seconds(min(reading.time_left(), READING_SECONDS)),
            input_bytes=base64.b64encode(payload.encode("utf-8")),
            env=reading.environment,
            output_limit=POWERSHELL_OUTPUT_LIMIT,
            tree=process_tree(hidden_window=True),
        )
    except subprocess.TimeoutExpired:
        reading.cannot_read(f"pwsh did not read the command in what was left of the guard's {READING_SECONDS} seconds")
        return None
    except OSError as exc:
        reading.cannot_read(f"could not start pwsh ({exc})")
        return None
    except json_writer.JsonWriteRefused as exc:
        reading.cannot_read(f"could not hand pwsh the command ({exc})")
        return None
    try:
        output = json_reader.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        reason = flat_text(result.stderr.decode("utf-8", "replace")) or f"exit code {result.returncode}"
        reading.cannot_read(f"pwsh could not read the command ({reason})")
        return None
    if not isinstance(output, dict):
        reading.cannot_read("pwsh answered with something other than the reader's object")
        return None
    if "error" in output:
        # PowerShell parses a whole script before it runs any of it, so a command that does not parse runs
        # nothing; a Bash command's lines before a syntax error do run.
        reading.notes.append(f"PowerShell would not parse the command, so none of it runs ({output['error']})")
        return None
    return output


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a PowerShell command through PowerShell's own parser: understood only when every node of its tree is one
    POWERSHELL_UNDERSTOOD names, and then read statement by statement as Bash's commands are."""
    del depth  # PowerShell nests nothing the guard reads
    output = powershell_events(command, reading)
    if output is None:
        return
    nodes, statements, commands = output.get("nodes"), output.get("statements"), output.get("commands")
    if not isinstance(nodes, list) or not isinstance(commands, list):
        reading.cannot_read("pwsh answered without the nodes and commands the reader lists")
        return
    unread = [str(node) for node in nodes if node not in POWERSHELL_UNDERSTOOD]
    for node in unread:
        reading.cannot_read(f"{node}: {NOT_READ.get(f'PowerShell {node}', 'a node type the guard does not know')}")
    reader = PowerShellReader(reading, commands)
    unread.extend(reader.read_forms(output))
    if unread or not isinstance(statements, list):
        # Each git call is still read, from a directory only a command list the guard reads settles.
        moves = any(reader.may_move(command) for command in commands)
        for command in commands:
            reader.read_git_only(command, None if moves else cwd)
        return
    directory = Directory(cwd)
    for statement in statements:
        directory = reader.read_statement(statement, directory)
    if reader.redirections_read < output.get("fileRedirections", 0):
        # A file redirection no statement read may write anywhere, a hub among the places.
        reading.cannot_read("a file redirection the reader did not reach, which may write in a hub")


class PowerShellReader:
    """Reads the statements of a PowerShell command the guard understands, as BashReader reads Bash's."""

    def __init__(self, reading: Reading, commands: list[Any]) -> None:
        self.reading = reading
        self.commands = commands
        self.redirections_read = 0

    def read_forms(self, output: Mapping[str, Any]) -> list[str]:
        """Tells the reading of each understood node type's form that is not read: an assignment to the environment
        or to PowerShell's own variable, a static member or one the text does not name, a cast to a type
        POWERSHELL_CASTS does not name, and a script block handed to a command POWERSHELL_BLOCK_COMMANDS does not
        name; returns them."""
        found = [
            f"assigns {name if isinstance(name, str) else 'something other than a plain variable'}, which "
            "PowerShell or git reads, or which the guard does not follow"
            for name, qualified in (
                (assigned.get("name"), assigned.get("qualified")) if isinstance(assigned, dict) else (None, False)
                for assigned in output.get("assigned") or []
            )
            if not isinstance(name, str) or qualified or name.lower() in POWERSHELL_VARIABLES
        ]
        found.extend(
            "a static member, or one the text does not name, which may run any code"
            for member in output.get("members") or []
            if not isinstance(member, dict) or member.get("static") or not member.get("constant")
        )
        found.extend(
            f"a cast or a type the guard does not read ({name}), whose conversion may run code"
            for name in output.get("types") or []
            if not isinstance(name, str) or name.lower().removeprefix("system.") not in POWERSHELL_CASTS
        )
        found.extend(
            f"a script block handed to {command or 'no command'}, which may run it as anything"
            for command in output.get("blocks") or []
            if POWERSHELL_COMMANDS.get(command.lower() if isinstance(command, str) else "")
            not in POWERSHELL_BLOCK_COMMANDS
        )
        for reason in found:
            self.reading.cannot_read(reason)
        return found

    def element(self, element: Any) -> list[Word]:
        """A command element's words: its text, a list's each, or None where the text does not settle it."""
        if isinstance(element, dict) and element.get("kind") == "words":
            texts = element.get("texts")
            if isinstance(texts, list) and all(isinstance(text, str) for text in texts):
                return list(texts)
        if not isinstance(element, dict) or element.get("kind") not in {"word", "parameter"}:
            return [None]
        text = element.get("text")
        if not isinstance(text, str):
            return [None]
        if "argument" in element:
            # A native program is handed -Name:value as one word; one whose value the text does not settle is too.
            value = self.element(element.get("argument"))
            return [text + value[0] if len(value) == 1 and value[0] is not None else None]
        return [text]

    def attached(self, command: Any) -> list[Any]:
        """The value attached by a colon to each parameter of a command, as -MemberName:Delete attaches Delete."""
        elements = command.get("elements") if isinstance(command, dict) else None
        return [
            element.get("argument")
            for element in (elements if isinstance(elements, list) else [])[1:]
            if isinstance(element, dict) and element.get("kind") == "parameter" and "argument" in element
        ]

    def values(self, command: Any) -> list[Word]:
        """Every value a cmdlet is given, by position or attached to a parameter by a colon, as -Path:<value>; None
        for one the text does not settle; a parameter written alone gives none, its value being the next element."""
        found: list[Word] = []
        for element in (command.get("elements") or [None])[1:]:
            if not isinstance(element, dict):
                found.append(None)
            elif element.get("kind") == "parameter":
                if "argument" in element:
                    found.extend(self.element(element.get("argument")))
            elif element.get("kind") != "block":
                found.extend(self.element(element))
        return found

    def words(self, command: Any) -> list[Word]:
        elements = command.get("elements") if isinstance(command, dict) else None
        words: list[Word] = [
            word for element in (elements if isinstance(elements, list) else []) for word in self.element(element)
        ]
        for word in words[1:]:
            if word is not None:
                self.reading.named.extend(named_paths(word))
        return words or [None]

    def may_move(self, command: Any) -> bool:
        """Whether a command may move the session's location: one that names a location cmdlet, or whose name the
        text does not settle; an expression standing as a command moves nothing."""
        if isinstance(command, dict) and command.get("expression"):
            return False
        name = self.words(command)[0]
        return name is None or name.lower() in {**POWERSHELL_LOCATION, **POWERSHELL_PUSH, **POWERSHELL_POP}

    def read_git_only(self, command: Any, cwd: Path | None) -> None:
        words = self.words(command)
        if words[0] is not None and command_name(words[0]) in GIT_NAMES:
            read_git(words[1:], cwd, self.reading)

    def read_statement(self, statement: Any, directory: Directory) -> Directory:
        pipelines = statement.get("pipelines") if isinstance(statement, dict) else None
        operators = statement.get("operators") if isinstance(statement, dict) else None
        nested = statement.get("nested") if isinstance(statement, dict) else None
        if not isinstance(pipelines, list) or not isinstance(operators, list) or not isinstance(nested, list):
            self.reading.cannot_read("a PowerShell statement the reader could not list")
            return UNKNOWN
        # A command inside the statement's expressions runs before it, in the session, so one that may move the
        # location leaves it unknown for the statement and after it.
        inside = self.read_pipeline(nested, directory).merged() if nested else directory
        if not same_place(inside.cwd, directory.cwd) or any(
            self.may_move(self.commands[index])
            for index in nested
            if isinstance(index, int) and index < len(self.commands)
        ):
            directory = UNKNOWN
        if not pipelines:
            # An assignment, an if, or a foreach: what it runs is all nested, read above.
            return directory
        outcome = self.read_pipeline(pipelines[0], directory)
        for operator, pipeline in zip(operators, pipelines[1:], strict=False):
            if operator == "&&":
                after = self.read_pipeline(pipeline, outcome.ok)
                outcome = Outcome(after.ok, joined(outcome.fail, after.fail))
            else:
                after = self.read_pipeline(pipeline, outcome.fail)
                outcome = Outcome(joined(outcome.ok, after.ok), after.fail)
        return directory if statement.get("background") else outcome.merged()

    def read_redirections(self, command: Mapping[str, Any], directory: Directory) -> None:
        """The one reading of a command's file redirections, whatever the command, through read_written; each is
        counted, so read_powershell can hold the count to every file redirection the tree holds."""
        for redirection in command.get("redirections") or []:
            if isinstance(redirection, dict) and redirection.get("kind") == "file":
                self.redirections_read += 1
                target = redirection.get("target")
                if isinstance(target, dict) and target.get("kind") == "null":
                    continue  # > $null discards the output and writes no file
                read_written(self.element(redirection.get("target"))[0], directory, self.reading)

    def read_pipeline(self, pipeline: Any, directory: Directory) -> Outcome:
        indexes = pipeline if isinstance(pipeline, list) else []
        commands = [self.commands[index] for index in indexes if isinstance(index, int) and index < len(self.commands)]
        if len(commands) == 1:
            return self.read_command(commands[0], directory)
        moved = False
        for index, command in enumerate(commands):
            moved = self.may_move(command) or moved
            self.read_command(command, directory, piped=index > 0)
        # Every command of a PowerShell pipeline runs in the session, so a location one sets outlives it.
        return certain(UNKNOWN if moved else directory)

    def read_command(self, command: Any, directory: Directory, piped: bool = False) -> Outcome:
        """One command of a pipeline; piped says an earlier command of it hands this one its input."""
        self.reading.time_left()
        if not isinstance(command, dict):
            return certain(directory)
        self.read_redirections(command, directory)
        if command.get("expression"):
            return certain(directory)
        if command.get("invocation") == "Dot":
            self.reading.cannot_read("a command dot-sourced into the session, which may define anything")
            return certain(UNKNOWN)
        words = self.words(command)
        name = words[0]
        if name is None:
            self.reading.cannot_read(
                "a command name the text does not settle, which may be any command, git among them"
            )
            return certain(UNKNOWN)
        # A name both a cmdlet's alias and a program PROGRAM_OPTIONS reads, as sort, or one WRITING_PROGRAMS names, as
        # cp, is the program where pwsh gives it no alias, off Windows.
        program = command_name(name) or ""
        unaliased = not platform_support.POWERSHELL_ALIASES_SHADOW_PROGRAMS and (
            program in PROGRAM_OPTIONS or (writer_name(program) is not None and name.lower() in POWERSHELL_ALIASES)
        )
        if name.lower() in POWERSHELL_COMMANDS and not unaliased:
            elements = command.get("elements") or []
            if POWERSHELL_COMMANDS[name.lower()] == "foreach-object" and any(
                not isinstance(element, dict) or element.get("kind") not in {"block", "parameter"}
                for element in [*elements[1:], *self.attached(command)]
            ):
                # ForEach-Object given a name rather than a script block calls the member by that name.
                self.reading.cannot_read("ForEach-Object given a member's name runs that member, which may run code")
                return certain(UNKNOWN)
            return self.read_cmdlet(name, words[1:], directory, self.values(command), command, piped)
        if powershell_command(name) and not unaliased:
            self.reading.cannot_read(f"runs {name}, a PowerShell command the guard does not read, which may run git")
            return certain(UNKNOWN)
        called = program
        built, arguments = multicall(built_name(called), words[1:])
        if built is None:
            self.reading.cannot_read(f"{called} runs a utility the text does not settle, which may be any command")
            return certain(UNKNOWN)
        return self.read_program(built, arguments, directory, False if built != called else None)

    def read_program(
        self, program: str, arguments: list[Word], directory: Directory, windows_build: bool | None
    ) -> Outcome:
        """A native program a command starts, by the name a table keeps for it (built_name), read as the Bash reader's
        read_program reads one."""
        if program in GIT_NAMES:
            # A native program is handed ~ as written; whether pwsh expands it first differs by release.
            handed = [None if word is not None and word.startswith("~") else word for word in arguments]
            read_git(handed, directory.cwd, self.reading)
        elif program == "gh":
            read_gh(arguments, directory, self.reading)
        elif program in RUNNERS:
            # The guard reads a bash or pwsh script only from Bash, so from PowerShell each is refused whatever it runs.
            return read_runner_words(program, arguments, directory, self.reading, lambda: RUNNERS[program], native=True)
        elif program in PROGRAM_OPTIONS:
            reason = program_refusal(program, arguments, self.reading.environment, windows_build)
            if reason is not None:
                self.reading.cannot_read(reason)
                return certain(UNKNOWN)
            for value, pattern in map(native_written, program_writes(program, arguments)):
                read_written(value, directory, self.reading, program_writer(program), pattern)
        elif writer_name(program) is not None:
            written = [native_written(word) for word in arguments]
            read_program_writes(program, written, directory, self.reading)
        elif program not in READ_ONLY_PROGRAMS:
            read_unlisted(program, [native_written(word) for word in arguments], directory, self.reading)
        return certain(directory)

    def read_cmdlet(
        self,
        name: str,
        arguments: list[Word],
        directory: Directory,
        values: list[Word],
        command: Any = None,
        piped: bool = False,
    ) -> Outcome:
        """A command POWERSHELL_COMMANDS names: one that moves the location is followed, one that writes has each path
        it is given judged (read_cmdlet_writes), and any other changes nothing the guard reads, unless a path it is
        given lies on a provider other than the file system (provider_refusal)."""
        cmdlet = POWERSHELL_COMMANDS[name.lower()]
        reason = provider_refusal(name, cmdlet, values)
        if reason is not None:
            self.reading.cannot_read(reason)
            return certain(UNKNOWN)
        if cmdlet in POWERSHELL_WRITING_COMMANDS:
            self.read_cmdlet_writes(name, cmdlet, command, directory, piped)
        if cmdlet == "set-location":
            return self.read_location(arguments, directory, push=False)
        if cmdlet == "push-location":
            return self.read_location(arguments, directory, push=True)
        if cmdlet == "pop-location":
            if arguments:
                return certain(Directory(None, directory.stack[1:]))
            if not directory.stack:
                return certain(directory)
            return certain(Directory(directory.stack[0], directory.stack[1:]))
        return certain(directory)

    def cmdlet_parameters(self, command: Any, cmdlet: str) -> tuple[list[tuple[str, list[Word]]], list[Word]]:
        """The values a cmdlet is given, each parameter that takes one with its name (powershell_parameter) and the
        values bound to it, attached by a colon or the element after it, and the values given by position."""
        elements = command.get("elements") if isinstance(command, dict) else None
        named: list[tuple[str, list[Word]]] = []
        positional: list[Word] = []
        pending = False
        for element in (elements if isinstance(elements, list) else [])[1:]:
            if isinstance(element, dict) and element.get("kind") == "parameter":
                kind, parameter = powershell_parameter(str(element.get("text")), cmdlet)
                pending = False
                if kind == "switch":
                    continue
                named.append((parameter, self.element(element.get("argument")) if "argument" in element else []))
                pending = "argument" not in element
            elif isinstance(element, dict) and element.get("kind") == "block":
                pending = False
            elif pending:
                named[-1][1].extend(self.element(element))
                pending = False
            else:
                positional.extend(self.element(element))
        return named, positional

    def read_cmdlet_writes(self, name: str, cmdlet: str, command: Any, directory: Directory, piped: bool) -> None:
        """The paths a cmdlet POWERSHELL_WRITING_COMMANDS names writes, reads, or creates, each judged as a
        redirection's target is: every value given to a parameter POWERSHELL_PATH_PARAMETERS names, New-Item's -Name
        joined to each path it is given, and every value given by position but one POWERSHELL_POSITIONS binds to a
        parameter that takes no path. A cmdlet after the first of a pipeline given no path, but Copy-Item, whose path
        is what it copies, binds the one it writes from its input, and New-Item given an item type that may make a
        link makes one (LINK_PROGRAMS): each is a part the guard does not read."""
        named, positional = self.cmdlet_parameters(command, cmdlet)
        bound = {"path" if parameter in POWERSHELL_PATH_BINDING else parameter for parameter, _ in named}
        slots = [slot for slot in POWERSHELL_POSITIONS[cmdlet] if slot not in bound]
        paths: list[tuple[Word, bool]] = []
        for index, value in enumerate(positional):
            slot = slots[index] if index < len(slots) else "path"
            if slot not in POWERSHELL_DATA_PARAMETERS:
                paths.append((value, False))
                bound.add(slot)
        names: list[Word] = []
        for parameter, values in named:
            if parameter == "name":
                names.extend(values)
            elif parameter in POWERSHELL_PATH_PARAMETERS:
                paths.extend((value, parameter in POWERSHELL_LITERAL_PATHS) for value in values)
            elif parameter == "itemtype":
                for value in values or [None]:
                    if value is None or not any(kind.startswith(value.lower()) for kind in POWERSHELL_ITEM_TYPES):
                        given = "an item type the text does not settle" if value is None else f"the item type {value}"
                        self.reading.cannot_read(
                            f"{name} is given {given}, which may make a link, through which a later write reaches a "
                            "place the guard cannot follow"
                        )
                        return
        if piped and cmdlet != "copy-item" and "path" not in bound:
            self.reading.cannot_read(f"{name} takes the path it writes from its input, which the text does not settle")
            return
        if "destination" in POWERSHELL_POSITIONS[cmdlet] and "destination" not in bound:
            # With no destination, Copy-Item and Move-Item write into the current location.
            paths.append((".", True))
        if names:
            # -Name names the item New-Item makes inside each path it is given, or the current location.
            parents = [each for each, _ in paths if each is not None] or ["."]
            paths = [
                (None if value is None else str(Path(platform_support.from_shell_path(parent)) / value), True)
                for value in names
                for parent in parents
            ]
        for value, literal in paths:
            self.read_cmdlet_path(name, value, literal, directory)

    def read_cmdlet_path(self, name: str, value: Word, literal: bool, directory: Directory) -> None:
        """One path a writing cmdlet is given: a leading ~ is home, as the file-system provider reads it, and a wildcard
        in one that is no literal path makes it a pattern, judged by the folder its matches lie in."""
        writer = program_writer(name)
        if value is not None and (value == "~" or value.startswith(("~/", "~\\"))):
            start = home(self.reading.environment)
            value = None if start is None else str(start / value[2:])
        wildcard = None if literal or value is None else POWERSHELL_WILDCARDS.search(value)
        if wildcard is not None and value is not None:
            read_written(pattern_folder(value[: wildcard.start()]), directory, self.reading, writer, pattern=True)
        else:
            read_written(value, directory, self.reading, writer)

    def read_location(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """Set-Location or Push-Location to a literal file-system path, by -Path, -LiteralPath, or position; a leading
        ~ is home, and - and + leave the location unknown, as a path the text does not settle does."""
        if arguments and arguments[0] is not None and arguments[0].lower() in {"-path", "-literalpath"}:
            arguments = arguments[1:]
        stack = (directory.cwd, *directory.stack) if push else directory.stack
        start = home(self.reading.environment)
        if not arguments:
            return certain(Directory(None, stack)) if start is None else moved_to(directory, start, stack)
        target = arguments[0]
        if target is not None and target.startswith("-") and target != "-":
            self.reading.cannot_read(f"Set-Location's parameter {target}, which the guard does not read")
            return certain(UNKNOWN)
        if len(arguments) > 1 or target is None or target in {"-", "+"}:
            return certain(Directory(None, stack))
        if target == "~" or target.startswith(("~/", "~\\")):
            if start is None:
                return certain(Directory(None, stack))
            path = start / target[2:]
        else:
            path = Path(target)
        if not path.is_absolute():
            if directory.cwd is None:
                return certain(Directory(None, stack))
            path = directory.cwd / path
        return moved_to(directory, path, stack)


ShellReader = Callable[[str, "Path | None", Reading, int], None]
# Monitor's command runs in the shell the Bash tool uses, so the guard reads it as Bash.
SHELL_READERS: dict[str, ShellReader] = {"Bash": read_bash, "Monitor": read_bash, "PowerShell": read_powershell}


def read_command(tool: str, command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """Every git call a shell tool's command makes, with where each acts, and what could not be read."""
    reading = start_reading(command, cwd, environment)
    read_into(reading, tool, cwd, environment)
    return reading


def read_into(reading: Reading, tool: str, cwd: Path, environment: Mapping[str, str]) -> None:
    """Reads the reading's command with the tool's shell reader, its hub lookups held to the reading time."""
    with lookups_within(reading.deadline):
        SHELL_READERS[tool](reading.text, cwd, reading, 0)


def start_reading(command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """A reading of one command, with the guarded hubs in view of the session, before any of it is read."""
    deadline = reading_clock() + READING_SECONDS
    guarded_hub_at.cache_clear()
    guarded_hub_of_repository.cache_clear()
    # The hubs in view decide whether a command the guard cannot read in time is refused, so their lookups keep
    # GUARD_GIT_TIMEOUT_SECONDS for each git call rather than end with the reading time; the session's own directories
    # bound them, never the command.
    project = environment.get("CLAUDE_PROJECT_DIR")
    places = [cwd, *([Path(platform_support.from_shell_path(project))] if project else [])]
    return Reading(environment, command, context_hubs=hubs_in_view(cwd, environment), places=places, deadline=deadline)


def reached_hubs(reading: Reading, found: set[Path] | None = None) -> set[Path]:
    """The guarded hubs a reading's settled places name, each looked up within the reading time, added to found.

    A place git answers with an error for, or whose git call the reading time cuts short, may be a guarded hub's, and
    is never read as a place outside every repository: every other place is still looked up, so the hubs it reaches
    join found, and then the first such error is raised."""
    found = set() if found is None else found
    failures: list[GitError] = []
    with lookups_within(reading.deadline):
        for call in reading.calls:
            reading.time_left()
            try:
                hub, _ = reached_hub(call.place)
            except GitError as exc:
                failures.append(exc)
                continue
            if hub is not None:
                found.add(hub)
    if failures:
        raise failures[0]
    return found


def named_hubs(reading: Reading, cwd: Path, found: set[Path]) -> set[Path]:
    """The guarded hubs the paths a command names lie in, looked up when none is in view and the command holds a part
    the guard does not read, which may reach one of them; at most NAMED_PATH_LIMIT directories, each within the
    reading time. A path git answers with an error for may be a guarded hub's, raised once the others are looked up;
    then, where the command names more directories than the limit, LookupsCutShort is raised, since one not looked up
    may be a guarded hub's."""
    directories: list[Path] = []
    for name in reading.named:
        try:
            directory = worktrees.existing_directory(resolve(name, cwd))
        except (GitError, ValueError):
            continue  # text that is no path the system can look at names no hub
        if directory is not None and not any(same_path(directory, each) for each in directories):
            directories.append(directory)
    failures: list[GitError] = []
    with lookups_within(reading.deadline):
        for directory in directories[:NAMED_PATH_LIMIT]:
            reading.time_left()
            try:
                hub = guarded_hub_at(directory)
            except GitError as exc:
                failures.append(exc)
                continue
            if hub is not None:
                found.add(hub)
    if failures:
        raise failures[0]
    if len(directories) > NAMED_PATH_LIMIT:
        raise LookupsCutShort(
            f"the command names {len(directories)} directories, more than the {NAMED_PATH_LIMIT} the guard looks up "
            "for a guarded hub"
        )
    return found


# ---------------------------------------------------------------------------------------------------------------
# The hook


def file_tool_refusal(tool: str, path: Path, cwd: Path, environment: Mapping[str, str]) -> str | None:
    """Why a file tool's edit outside every guarded hub is refused: while a hub of the session's own directory or
    project is guarded, an edit of a file git reads its system or global configuration from, as a redirection's write
    to one is (Judge.configuration_write_refusal), since a setting written there reaches every git a later command
    starts."""
    hubs = hubs_in_view(cwd, environment)
    if not hubs:
        return None
    return Judge(hubs, environment, [], math.inf).configuration_write_refusal(path, f"The {tool} tool writes")


def guard(stream: str) -> int:
    # The command and the guarded hubs in view, known before the command is read, so that a reader or a judgment that
    # fails leaves a command the guard cannot read; None until then.
    in_view: tuple[str, set[Path]] | None = None
    # Whether the hubs of the places the command names are being looked up: a lookup the reading time cuts short, one
    # git answers with an error for, or paths past NAMED_PATH_LIMIT leave a place not looked up, which may be a guarded
    # hub's.
    looking_up = False
    # The guard judges each call from the environment it was started with, and its own git calls run without the
    # variables that would have git write a trace or its output into a file they name.
    environment = dict(os.environ)
    withhold_written_variables(os.environ)
    try:
        event = json_reader.loads(stream)
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
            else:
                reason = file_tool_refusal(tool, resolve(target, cwd), cwd, environment)
                refused = None if reason is None else f"{reason} {DENY_REASON}"
        elif tool in SHELL_READERS and not isinstance(tool_input.get("command"), str):
            print(f"hub guard skipped: the {tool} event names no command", file=sys.stderr)
        elif tool in SHELL_READERS:
            reading = start_reading(tool_input["command"], cwd, environment)
            hubs = set(reading.context_hubs)
            in_view = (reading.text, hubs)
            read_into(reading, tool, cwd, environment)
            looking_up = True
            reached_hubs(reading, hubs)
            if (reading.unread or reading.refusals or any(run.unread for run in reading.scripts)) and not hubs:
                named_hubs(reading, cwd, hubs)
            looking_up = False
            reason = denied(reading, hubs)
            # A refusal with a remedy of its own is no hub edit a worktree would take, so it goes without DENY_REASON.
            refused = reason if reason is None or isinstance(reason, OwnRemedy) else f"{reason} {DENY_REASON}"
            for note in reading.notes:
                print(f"hub guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not block every tool; the regression suite pins what it refuses
        cut_short = looking_up and isinstance(exc, (ReadingTimeout, GitError, LookupsCutShort))
        if in_view is None or not (in_view[1] or cut_short):
            print(f"hub guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        # Where a guarded hub is in view, a command the guard failed to read or judge is one it does not read,
        # whether a reader fails on nesting past Python's recursion limit or on any other error.
        refused = (
            f"The guard failed reading this command ({type(exc).__name__}: {exc}), so it is refused while a hub is "
            f"guarded. {REWRITE_IT} {DENY_REASON}"
        )
    if refused is not None:
        # A reason JSON cannot carry, such as one naming a path whose name decoded to a lone surrogate, gives way to
        # UNWRITABLE_REASON, so a refusal is always written: an error instead would let the command through.
        print(hook_decision.denial(refused, UNWRITABLE_REASON))
    return 0


def main() -> int:
    return guard(sys.stdin.read())


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")

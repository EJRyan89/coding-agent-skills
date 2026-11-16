"""The hub guard: the Claude Code PreToolUse hook .claude/settings.json runs, which reads the hook JSON on stdin.

Usage:
  python tools/hub_guard.py   reads one PreToolUse event on stdin and prints a deny decision, or nothing

The guard catches the accidents a session makes in the hub, and is no sandbox: while a hub is guarded it refuses an
Edit, Write, or NotebookEdit whose target lies in the hub, a git call that acts on the hub in any form but the read-only
ones READ_ONLY_FORMS declares, and a redirection whose target lies in the hub, whether the command runs through the Bash
tool, the PowerShell tool, or the Monitor tool, whose command runs in Bash. It reads a command for its git calls, where
each acts, and its redirections, Bash by ejryan89-skill-core's bash_syntax.py grammar and PowerShell by PowerShell's own
parser (tools/hub_guard-powershell.ps1); every other part is a program the guard does not read, which it allows, and
ejryan89-skill-core's hub_state.py check, which the shepherd and repo-cleanup run, reports a hub a missed write left
changed.
docs/parallel-sessions.md ("The guard's contract") states the contract. Every decision is made here. It finds the
checkout that holds a path, and runs git, through tools/worktrees.py, which owns the hub and its worktrees. It is inert
unless `git config coding-agent-skills.hubGuard true` was set in this clone.

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

import bash_syntax
import command_wrappers
import executable_locator
import file_presence
import hook_decision
import json_reader
import json_writer
import whole_number
from bounded_process import run_bounded
from console import run_main
from flat_text import reason_line
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
from hub_state import GUARD_SETTING
from job_tree import process_tree
from option_reader import Word
from path_keys import name_key
from powershell_startup import POWERSHELL_PROGRAMS, UnknownWord, startup_command
from text_lines import text_lines

from deployer import platform_support
from tools import worktrees
from tools.worktrees import CLIENT, GUARD_GIT_TIMEOUT_SECONDS, MAIN, Checkout, same_path

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
# read and which may run git: refused while a hub is guarded.
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
# What the readers follow
#
# The guard reads a command for its git calls, where each acts, and the files its redirections write; any other part
# of it is a program the guard does not read, which it allows ("The guard's contract" in docs/parallel-sessions.md).


# The blanks before a # that make a line of the PowerShell reader a comment (powershell_reader): the ones its lines
# are indented with.
COMMENT_INDENT = " \t"
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

# The Bash builtins that may move the shell in a way the guard does not follow: each leaves where the shell is after
# it unknown, so a git call placed after it is judged as one whose place the text does not settle. cd, pushd, and popd
# are followed, `command -v` and `command -V` only print, and every other builtin moves nothing.
BUILTINS_NOT_FOLLOWED = {
    **dict.fromkeys((".", "source"), "runs a file's commands"),
    "eval": "runs text as commands",
    **dict.fromkeys(("builtin", "command"), "runs the command its next word names, cd among them"),
    "trap": "runs text as commands when a signal or a command fires it",
    "fc": "runs commands again from the history",
    **dict.fromkeys(("alias", "unalias", "enable", "hash"), "changes what a later name runs"),
}
# The names beyond GIT_* that git reads to choose a file it reads or writes, or a program it runs, each with why; an
# assignment to one before git is a part of the git call the guard does not settle (git_reads). A name git reads only
# for its messages, as LANG or LC_ALL, chooses neither.
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

GLOB = re.compile(r"[*?]|\[.*\]")
BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")

# The PowerShell AST node types (System.Management.Automation.Language, by reflection) the guard understands: a
# list of pipelines of commands, joined by && and ||, whose elements are constants, strings without expansions, and
# parameters, with redirections. A command holding any other type has only its git calls read, each placed where the
# text does not settle once any command in it may move the location (read_powershell).
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
# A PowerShell path on a provider other than the file system: a drive whose name is more than one letter, such as
# env:, alias:, or HKLM:, or a provider-qualified path such as Environment::. A location there is no checkout.
PROVIDER_PATH = re.compile(r"^(?:(?:[\w.]+\\)?[A-Za-z]\w*::|[A-Za-z][\w-]+:)")
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
    "Write the git call with literal words and run it again; the guard's contract in docs/parallel-sessions.md lists "
    "what it reads."
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
        said = reason_line(result.stderr) or f"git config exited with {result.returncode}"
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
    """A file a redirection writes at a path its text names, and what a refusal says writes it, as `The command
    redirects its output into`; for a pattern, path is the folder every file it matches lies in."""

    path: Path
    opening: str
    pattern: bool = False


@dataclass
class Reading:
    """What the guard read from one command: the git calls it makes, in order, the files its redirections write, the
    parts of a git call's own words the guard does not settle, which are refused while a guarded hub is in view, and
    notes on what it passed over. Any other part it does not read is allowed."""

    environment: Mapping[str, str]
    text: str = ""
    calls: list[GitCall] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    context_hubs: set[Path] = field(default_factory=set)
    # The session's own directory and project, where git may read the configuration otherwise than the guard does.
    places: list[Path] = field(default_factory=list)
    # When the guard's reading time ends, by reading_clock.
    deadline: float = math.inf

    def cannot_read(self, what: str) -> None:
        """A part of a git call's own words the guard does not settle: refused while a guarded hub is in view."""
        if what not in self.unread:
            self.unread.append(what)

    def time_left(self) -> float:
        """The seconds left of the reading time; past it, raises ReadingTimeout, which the hook refuses as a failure
        of the guard's own when the command names git while a guarded hub is in view."""
        return time_left(self.deadline)


class ReadingTimeout(Exception):
    """The guard's reading time ended before the command was read and judged."""


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
        said = reason_line(result.stderr) or f"git for-each-ref exited with {result.returncode}"
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
        if reading.unread:
            if self.hubs:
                return (
                    f"The guard does not settle part of a git call in this command ({reading.unread[0]}), so it is "
                    f"refused while a hub is guarded. {REWRITE_IT}"
                )
            self.notes.extend(reading.unread)
        writes = list(reading.writes)
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
        """Why a file a redirection writes at a path the text names is refused: one in a guarded hub's tracked files or
        anywhere in its git directory, a link followed, and, while a hub is guarded, one git reads its system or global
        configuration from. For a pattern, the folder its match lies in is judged."""
        opening = f"{write.opening} the file a pattern matches in" if write.pattern else write.opening
        if hub_protected(write.path):
            return f"{opening} {write.path}, {'a folder' if write.pattern else 'a file'} of a guarded hub."
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
                return self.unreadable(call, reason_line(failure_message(arguments, result)))
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


def git_reads(name: str) -> bool:
    """Whether git reads a variable to choose where it acts, a file it reads or writes, or a program it runs: a GIT_
    name, or one GIT_READ_NAMES names."""
    canonical = folded(name)
    return canonical.startswith("GIT_") or canonical in GIT_READ_NAMES


# ---------------------------------------------------------------------------------------------------------------
# The Bash reader


@dataclass(frozen=True)
class BashStart:
    """What a bash takes from the environment it starts with that the reader needs: its home, whether it searches
    CDPATH, and the functions it is handed by name."""

    home: Path | None  # None where the home is not known, so a word or a directory holding it is unsettled
    cdpath: bool
    functions: frozenset[str]


def bash_start(reading: Reading) -> BashStart:
    """Reads the shell state the hook's environment hands the Bash tool's shell."""
    environment = reading.environment
    functions = frozenset(
        folded(match.group(1)) for match in (EXPORTED_FUNCTION.match(folded(name)) for name in environment) if match
    )
    return BashStart(home(environment), bool(environment.get("CDPATH")), functions)


class BashReader:
    """Reads a Bash command tree for the git calls it makes, where each acts, and the files its redirections write.

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
            self.reading.notes.append(f"substitutions nest more than {NESTING_LIMIT} deep, and the rest is not read")
            return UNKNOWN
        try:
            tree = bash_syntax.parse(text)
        except bash_syntax.BashSyntaxError as exc:
            self.reading.notes.append(f"Bash would not read it as written, or the guard cannot ({exc})")
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
            # A function may run cd under any name a later command calls, so where the shell is after one is unknown.
            return certain(UNKNOWN)
        self.read_redirects(node.redirects, directory)
        moves = changes_directory(node)
        inside = UNKNOWN if moves else directory
        if isinstance(node, bash_syntax.Loop):
            # An arithmetic for's header is expanded before the first pass, so its substitutions run.
            self.read_words([*(node.words or []), *node.header], directory)
        if isinstance(node, bash_syntax.Case):
            self.read_words([node.word, *node.patterns], directory)
        if isinstance(node, bash_syntax.Arithmetic):
            self.read_words([node.expression], directory)
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
        """The text Bash makes of a word, or None where only running settles it: an expansion, a glob, or a brace
        expansion."""
        pieces: list[str] = []
        for part in word.parts:
            if part.kind == "text":
                if not part.quoted and (GLOB.search(part.text) or BRACE.search(part.text)):
                    return None
                pieces.append(part.text)
            elif part.kind == "home" and self.start.home is not None:
                pieces.append(str(self.start.home))
            else:
                return None
        return "".join(pieces)

    def read_redirects(self, redirects: list[bash_syntax.Redirect], directory: Directory) -> None:
        for redirect in redirects:
            here = redirect.here
            if here is not None:
                for text in here.substitutions:
                    self.nested().read_text(text, directory)
                continue
            if redirect.target is None:
                continue
            value = self.read_words([redirect.target], directory)[0]
            if redirect.operator in {"<", "<<<"} or (
                redirect.operator in {"<&", ">&"} and value is not None and DESCRIPTOR_TARGET.fullmatch(value)
            ):
                continue
            if value is None:
                value, pattern = self.written_value(redirect.target)
                read_written(value, directory, self.reading, pattern)
            else:
                read_written(value, directory, self.reading)

    def written_value(self, word: bash_syntax.Word) -> tuple[Word, bool]:
        """A redirection's target, with whether it is a pattern: the text the word makes but for the system's temporary
        folder, named by a variable TEMPORARY_VARIABLES lists that the environment holds; for a word whose unquoted text
        holds a wildcard, the folder every file it matches lies in (pattern_folder); (None, False) where the text does
        not settle it, a brace expansion among them, which may name any path."""
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
        if index == len(words):
            return certain(directory)
        prefixes: list[tuple[str, Word]] = []
        for word in words[:index]:
            assignment = word.assignment
            if assignment is None:
                continue
            # An array's value, NAME=( ), or an element's, NAME[i]=, is one the text does not settle, which may still
            # reach git's environment under NAME, so it is judged by its name as any assignment before git is.
            whole = not word.array and not assignment.element
            prefixes.append(
                (assignment.name, self.word_value(bash_syntax.Word("", assignment.value)) if whole else None)
            )
        return self.read_command_words(values[index:], directory, prefixes)

    def read_git_prefixes(self, prefixes: list[tuple[str, Word]]) -> None:
        """The assignments before git, which its environment holds while it runs: one git reads to choose where it
        acts, what configuration it reads, or what program it runs (git_reads) is a part of the git call the guard
        does not settle, but for those GIT_PREFIXES names with a value it allows."""
        for name, value in prefixes:
            if name in GIT_PREFIXES:
                allowed = GIT_PREFIXES[name]
                if value is None or (allowed and value not in allowed):
                    self.reading.cannot_read(f"an assignment before git, {name}, which may change what git runs")
            elif git_reads(name):
                self.reading.cannot_read(
                    f"an assignment before git, {name}, which may change where it acts or what configuration it reads"
                )

    def read_command_words(
        self, values: list[Word], directory: Directory, prefixes: list[tuple[str, Word]] | None = None
    ) -> Outcome:
        """A command's words, its name first: git and a gh checkout are read as git calls, cd, pushd, and popd are
        followed, and a command that may move the shell in a way the guard does not follow leaves where it is unknown:
        a name the text does not settle, a function the environment hands the shell, and a builtin
        BUILTINS_NOT_FOLLOWED names. Any other command is a program the guard does not read."""
        name, arguments = values[0], values[1:]
        if name is None:
            return certain(UNKNOWN)
        program = command_name(name) or ""
        bare = "/" not in name and "\\" not in name
        if bare and folded(name) in self.start.functions:
            return certain(UNKNOWN)
        wrapper = command_wrappers.wrapper_name(program)
        if wrapper in command_wrappers.COMMAND_WRAPPERS and (bare or wrapper not in SHELL_WRAPPERS):
            return self.read_wrapped(wrapper, arguments, directory, prefixes or [])
        if bare and name in {*BUILTINS_NOT_FOLLOWED, "cd", "pushd", "popd"}:
            return self.read_builtin(name, arguments, directory)
        self.read_program(program, arguments, directory, prefixes or [])
        return certain(directory)

    def read_builtin(self, name: str, arguments: list[Word], directory: Directory) -> Outcome:
        """A builtin that moves the shell: cd, pushd, and popd, followed, and one BUILTINS_NOT_FOLLOWED names, which
        leaves where the shell is unknown."""
        if name in {"cd", "pushd"}:
            return self.read_cd(arguments, directory, push=name == "pushd")
        if name != "popd":
            return certain(UNKNOWN)
        if arguments:
            return certain(Directory(None, directory.stack[1:]))
        if not directory.stack:
            return certain(directory)
        return certain(Directory(directory.stack[0], directory.stack[1:]))

    def read_program(
        self, program: str, arguments: list[Word], directory: Directory, prefixes: list[tuple[str, Word]]
    ) -> None:
        """A program the command starts: git and a gh checkout read as git calls, and a literal bash -c or pwsh
        -Command script by its shell's reader, in a shell of its own, so what it changes dies with it. Any other
        program is one the guard does not read."""
        if program in GIT_NAMES:
            self.read_git_prefixes(prefixes)
            read_git(arguments, directory.cwd, self.reading)
        elif program == "gh":
            read_gh(arguments, directory, self.reading)
        elif program in command_wrappers.SCRIPT_SHELLS:
            script = shell_script(arguments)
            if script is not None:
                self.nested().read_text(script, directory)
        elif program in POWERSHELL_PROGRAMS and self.depth < NESTING_LIMIT:
            read_powershell_start(program, arguments, directory.cwd, self.reading, self.depth + 1)

    def read_wrapped(
        self, wrapper: str, arguments: list[Word], directory: Directory, prefixes: list[tuple[str, Word]]
    ) -> Outcome:
        """A wrapper COMMAND_WRAPPERS names, read as the command it runs, its assignments judged as ones before that
        command; one whose words do not settle its program is not read. Only `command` and `builtin` run their
        command in the shell itself, so only a cd they run moves it."""
        wrapped = wrapped_command(wrapper, arguments)
        if wrapped is None:
            return certain(UNKNOWN if wrapper in SHELL_WRAPPERS else directory)
        if not wrapped.words:
            return certain(directory)
        start = Directory(None, directory.stack) if wrapped.moved else directory
        outcome = self.read_command_words(wrapped.words, start, [*prefixes, *wrapped.assignments])
        return outcome if wrapper in SHELL_WRAPPERS else certain(directory)

    def read_cd(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """cd or pushd to a directory: a literal one settles where the shell goes; one the text does not settle, cd -,
        a CDPATH search, an option, and pushd's rotations leave it unknown."""
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        elif arguments and arguments[0] is not None and arguments[0].startswith(("-", "+")) and arguments[0] != "-":
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


# A word the text does not settle, handed to command_wrappers, which reads words as text: no word Bash makes holds NUL.
UNSETTLED_TEXT = "\0"
# Each wrapper's options that move the command it runs to another directory: env's -C and sudo's -D, and both
# --chdir; the git call such a command makes is placed where the text does not settle.
CHDIR_LETTERS = {"env": "C", "sudo": "D"}
CHDIR_LONG = "--chdir"
# The wrappers that run their command in the shell itself, so a cd there moves it, rather than in a process of their
# own; exec replaces the shell, so nothing after it runs.
SHELL_WRAPPERS = frozenset({"command", "builtin"})
# The wrappers that add words from their input to the command they run.
INPUT_WRAPPERS = frozenset({"xargs"})


@dataclass(frozen=True)
class Wrapped:
    """The command a wrapper runs (command_wrappers.unwrapped): its words, None for one the text does not settle; the
    assignments the wrapper hands it, as env's NAME=VALUE; and whether the wrapper moves it to another directory."""

    words: list[Word]
    assignments: list[tuple[str, Word]]
    moved: bool


def wrapped_command(wrapper: str, arguments: list[Word]) -> Wrapped | None:
    """The command a wrapper COMMAND_WRAPPERS names runs, read by the shared reader of every wrapper; None where the
    wrapper's words do not settle which program it runs, which is a program the guard does not read. An empty command
    is one that names none, or `command -v`, which only looks one up."""
    handed = [UNSETTLED_TEXT if word is None else word for word in arguments]
    words = command_wrappers.unwrapped([wrapper, *handed])
    if words and (words[0] == UNSETTLED_TEXT or words[0].startswith(command_wrappers.UNREAD_PROGRAM)):
        return None
    consumed = handed[: len(handed) - len(words)]
    assignments: list[tuple[str, Word]] = []
    for word in consumed:
        matched = command_wrappers.ASSIGNMENT.match(word)
        if matched is not None and UNSETTLED_TEXT not in word:
            name = word[: matched.end()].rstrip("+=").partition("[")[0]
            value = None if matched.group(1) else word[matched.end() :]
            assignments.append((name, value))
        elif matched is not None:
            assignments.append((word[: matched.end()].rstrip("+=").partition("[")[0], None))
    letter = CHDIR_LETTERS.get(wrapper)
    moved = any(
        word == CHDIR_LONG
        or word.startswith(f"{CHDIR_LONG}=")
        or (letter is not None and word.startswith("-") and not word.startswith("--") and letter in word[1:])
        for word in consumed
    )
    command = [None if word == UNSETTLED_TEXT else word for word in words]
    if command and wrapper in INPUT_WRAPPERS:
        command.append(None)  # the words its input adds, which the text does not settle
    return Wrapped(command, assignments, moved)


def read_powershell_start(program: str, arguments: list[Word], cwd: Path | None, reading: Reading, depth: int) -> None:
    """The script a pwsh or Windows PowerShell start-up runs, read by PowerShell's reader where its text is literal:
    -Command, -CommandWithArgs, and -EncodedCommand by any prefix PowerShell reads as one, past every other start-up
    parameter and its value, as ejryan89-skill-core's powershell_startup.startup_command reads them for every reader
    in the repository, and Windows PowerShell's script given by position. An -EncodedCommand, whose text is Base64,
    runs a script the text does not settle, so it is read as a git call whose subcommand the text does not settle,
    refused while a hub is guarded; a start-up that runs no script the words hold, and a script a word the text does
    not settle holds, are not read."""
    command = startup_command(arguments, positional_command=program == "powershell", positional_file=program == "pwsh")
    if command is None or isinstance(command, UnknownWord):
        return
    if command.encoded:
        reading.notes.append(f"{program} -EncodedCommand runs a script the text does not settle")
        read_git([None], cwd, reading)
        return
    script = arguments[command.start : command.start + 1 if command.with_arguments else len(arguments)]
    if not script or None in script or script == ["-"]:
        return
    read_powershell(" ".join(str(word) for word in script), cwd, reading, depth)


def shell_script(arguments: list[Word]) -> str | None:
    """The literal script a shell's -c runs (command_wrappers.shell_script_index); None for a shell started without
    one, or one whose script or options the text does not settle."""
    if None in arguments[:1] or any(word is None for word in arguments if word is not None and word.startswith("-")):
        return None
    index = command_wrappers.shell_script_index([UNSETTLED_TEXT if word is None else word for word in arguments])
    if index is None or any(word is None for word in arguments[:index]):
        return None
    return arguments[index]


REDIRECTION = "The command redirects its output into"


def read_written(value: Word, directory: Directory, reading: Reading, pattern: bool = False) -> None:
    """A file a redirection writes: one the text places in a guarded hub is refused, as a file tool's edit is
    (Judge.write_refusal); one whose path or directory the text does not settle is noted and allowed. For a pattern,
    value is the folder every file it matches lies in (pattern_folder)."""
    if value is None:
        reading.notes.append("a redirection writes a file the text does not settle")
        return
    if value in DEVICE_FILES:
        return
    path = Path(platform_support.from_shell_path(value))
    if not path.is_absolute() and directory.cwd is None:
        reading.notes.append(f"a redirection writes {value} in a directory the text does not settle")
    else:
        written = path if path.is_absolute() else (directory.cwd or Path()) / path
        reading.writes.append(Write(written, REDIRECTION, pattern))


def pattern_folder(prefix: str) -> str:
    """The folder every file a pattern matches lies in, given the text before its first wildcard: that text up to its
    last separator, which a wildcard never matches, or `.`, the directory the command runs in, where it holds none."""
    cut = max(prefix.rfind("/"), prefix.rfind("\\"))
    return "." if cut < 0 else prefix[: cut + 1]


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
    """gh, which runs git itself to check a branch out for the subcommands GH_CHECKOUTS names, in the checkout it runs
    in: read as a git call there. Its words are read up to its subcommand, past each flag GH_VALUED_FLAGS names with
    its value; a gh call whose subcommand the text does not settle, or that holds another flag before it, which cobra
    may read as taking the next word, as `gh pr -b x checkout 12` does, is a program the guard does not read."""
    named: list[Word] = []
    words = iter(arguments)
    for word in words:
        if len(named) == 2:
            break
        if word in GH_VALUED_FLAGS:
            next(words, None)
        elif word is None or not word.startswith("-"):
            named.append(word)
        elif not gh_flag_with_value(word):
            return
    if None not in named and tuple(named) in GH_CHECKOUTS:
        # gh runs git in its own directory with the environment it was given, so the inherited variables place it.
        values = git_variables(inherited_variables(reading.environment), reading.notes)
        place = GitOptions.initial(directory.cwd, values, opaque=False).place()
        reading.calls.append(GitCall(place, " ".join(str(word) for word in named), [], program="gh"))


def gh_flag_with_value(word: str) -> bool:
    """Whether a flag word of gh carries its value as one of GH_VALUED_FLAGS does: after = for a long one, or
    attached to -R."""
    name, equals, _ = word.partition("=")
    return (bool(equals) and name in GH_VALUED_FLAGS) or (word.startswith("-R") and len(word) > 2)


# A redirection's target that names a descriptor to duplicate or close, as in 2>&1 or >&-.
DESCRIPTOR_TARGET = re.compile(r"[0-9]+-?|-")


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
    """Whether a compound command holds a command that may change the shell's directory: cd, pushd, or popd, a builtin
    BUILTINS_NOT_FOLLOWED names, a function definition, or a command whose name the text does not settle."""
    if isinstance(node, bash_syntax.FunctionDefinition):
        return True
    if isinstance(node, bash_syntax.Simple):
        names = [word for word in node.words if word.assignment is None][:1]
        return bool(names) and names[0].literal in {"cd", "pushd", "popd", None, *BUILTINS_NOT_FOLLOWED}
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
    """What PowerShell's own parser makes of one command, or None, with a note saying why, when it could not be read,
    which leaves a command the guard does not read.

    pwsh starts with -Command rather than -File, so no execution policy applies, and not with -EncodedCommand,
    which endpoint protection treats as a sign of malware. The command travels base64-encoded on stdin, past any
    console code page.
    """
    pwsh = platform_support.find_pwsh(reading.environment.get("PATH", ""))
    if pwsh is None:
        reading.notes.append("could not find pwsh to read a PowerShell command")
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
        reading.notes.append(f"pwsh did not read the command in what was left of the guard's {READING_SECONDS} seconds")
        return None
    except OSError as exc:
        reading.notes.append(f"could not start pwsh ({exc})")
        return None
    except json_writer.JsonWriteRefused as exc:
        reading.notes.append(f"could not hand pwsh the command ({exc})")
        return None
    try:
        output = json_reader.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        reason = reason_line(result.stderr.decode("utf-8", "replace")) or f"exit code {result.returncode}"
        reading.notes.append(f"pwsh could not read the command ({reason})")
        return None
    if not isinstance(output, dict):
        reading.notes.append("pwsh answered with something other than the reader's object")
        return None
    if "error" in output:
        # PowerShell parses a whole script before it runs any of it, so a command that does not parse runs
        # nothing; a Bash command's lines before a syntax error do run.
        reading.notes.append(f"PowerShell would not parse the command, so none of it runs ({output['error']})")
        return None
    return output


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a PowerShell command through PowerShell's own parser, statement by statement as Bash's commands are,
    where every node of its tree is one POWERSHELL_UNDERSTOOD names in a form it follows (read_forms). Where any other
    stands in it, only its git calls and its redirections are read, each placed where the text does not settle once
    any command in it may move the location."""
    output = powershell_events(command, reading)
    if output is None:
        return
    nodes, statements, commands = output.get("nodes"), output.get("statements"), output.get("commands")
    if not isinstance(nodes, list) or not isinstance(commands, list):
        reading.notes.append("pwsh answered without the nodes and commands the reader lists")
        return
    reader = PowerShellReader(reading, commands, depth)
    followed = all(node in POWERSHELL_UNDERSTOOD for node in nodes) and not reader.read_forms(output)
    if not followed or not isinstance(statements, list):
        moves = any(reader.may_move(command) for command in commands)
        for command in commands:
            reader.read_git_only(command, Directory(None if moves else cwd))
        return
    directory = Directory(cwd)
    for statement in statements:
        directory = reader.read_statement(statement, directory)
    if reader.redirections_read < output.get("fileRedirections", 0):
        reading.notes.append("a file redirection the reader did not reach")


class PowerShellReader:
    """Reads the statements of a PowerShell command, as BashReader reads Bash's."""

    def __init__(self, reading: Reading, commands: list[Any], depth: int = 0) -> None:
        self.reading = reading
        self.commands = commands
        self.depth = depth
        self.redirections_read = 0

    def read_forms(self, output: Mapping[str, Any]) -> list[str]:
        """Each form of an understood node type that may run code the guard does not follow, which may move the
        location: an assignment to the environment or to PowerShell's own variable, a static member or one the text
        does not name, a cast to a type POWERSHELL_CASTS does not name, and a script block handed to a command
        POWERSHELL_BLOCK_COMMANDS does not name."""
        found = [
            f"assigns {name if isinstance(name, str) else 'something other than a plain variable'}"
            for name, qualified in (
                (assigned.get("name"), assigned.get("qualified")) if isinstance(assigned, dict) else (None, False)
                for assigned in output.get("assigned") or []
            )
            if not isinstance(name, str) or qualified or name.lower() in POWERSHELL_VARIABLES
        ]
        found.extend(
            "a static member, or one the text does not name"
            for member in output.get("members") or []
            if not isinstance(member, dict) or member.get("static") or not member.get("constant")
        )
        found.extend(
            f"a cast or a type the guard does not follow ({name})"
            for name in output.get("types") or []
            if not isinstance(name, str) or name.lower().removeprefix("system.") not in POWERSHELL_CASTS
        )
        found.extend(
            f"a script block handed to {command or 'no command'}"
            for command in output.get("blocks") or []
            if POWERSHELL_COMMANDS.get(command.lower() if isinstance(command, str) else "")
            not in POWERSHELL_BLOCK_COMMANDS
        )
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

    def words(self, command: Any) -> list[Word]:
        elements = command.get("elements") if isinstance(command, dict) else None
        words: list[Word] = [
            word for element in (elements if isinstance(elements, list) else []) for word in self.element(element)
        ]
        return words or [None]

    def may_move(self, command: Any) -> bool:
        """Whether a command may move the session's location: one that names a location cmdlet, or whose name the
        text does not settle; an expression standing as a command moves nothing."""
        if isinstance(command, dict) and command.get("expression"):
            return False
        name = self.words(command)[0]
        return name is None or name.lower() in {**POWERSHELL_LOCATION, **POWERSHELL_PUSH, **POWERSHELL_POP}

    def read_git_only(self, command: Any, directory: Directory) -> None:
        """A command's git call and its redirections, from a directory read_powershell settles or leaves unknown."""
        if isinstance(command, dict):
            self.read_redirections(command, directory)
        words = self.words(command)
        if words[0] is not None and not powershell_command(words[0]):
            self.read_native(words, directory)

    def read_statement(self, statement: Any, directory: Directory) -> Directory:
        pipelines = statement.get("pipelines") if isinstance(statement, dict) else None
        operators = statement.get("operators") if isinstance(statement, dict) else None
        nested = statement.get("nested") if isinstance(statement, dict) else None
        if not isinstance(pipelines, list) or not isinstance(operators, list) or not isinstance(nested, list):
            self.reading.notes.append("a PowerShell statement the reader could not list")
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
        counted, so read_powershell can note one the tree holds that no statement reached."""
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
        for command in commands:
            moved = self.may_move(command) or moved
            self.read_command(command, directory)
        # Every command of a PowerShell pipeline runs in the session, so a location one sets outlives it.
        return certain(UNKNOWN if moved else directory)

    def read_command(self, command: Any, directory: Directory) -> Outcome:
        """One command of a pipeline: git and a gh checkout are read as git calls, the location cmdlets are followed,
        and a command that may move the location in a way the guard does not follow leaves it unknown: one
        dot-sourced into the session, one whose name the text does not settle, ForEach-Object given a member's name,
        which it calls, and a name PowerShell keeps for its own commands that POWERSHELL_COMMANDS does not hold. Any
        other command is a program the guard does not read."""
        self.reading.time_left()
        if not isinstance(command, dict):
            return certain(directory)
        self.read_redirections(command, directory)
        if command.get("expression"):
            return certain(directory)
        name = self.words(command)[0]
        if command.get("invocation") == "Dot" or name is None:
            return certain(UNKNOWN)
        arguments = self.words(command)[1:]
        cmdlet = POWERSHELL_COMMANDS.get(name.lower())
        if cmdlet is not None:
            if cmdlet == "foreach-object" and any(
                not isinstance(element, dict) or element.get("kind") not in {"block", "parameter"}
                for element in [*(command.get("elements") or [])[1:], *self.attached(command)]
            ):
                return certain(UNKNOWN)
            return self.read_cmdlet(cmdlet, arguments, directory)
        if powershell_command(name):
            return certain(UNKNOWN)
        self.read_native([name, *arguments], directory)
        return certain(directory)

    def read_native(self, words: list[Word], directory: Directory) -> None:
        """A native program a command starts, read as the Bash reader reads one: git and a gh checkout as git calls, a
        wrapper as the command it runs, and a literal bash -c or pwsh -Command script by its shell's reader, each from
        a directory of its own, since a native program never moves the session's location."""
        program = command_name(words[0]) or ""
        arguments = words[1:]
        wrapper = command_wrappers.wrapper_name(program)
        if program in GIT_NAMES:
            # A native program is handed ~ as written; whether pwsh expands it first differs by release.
            handed = [None if word is not None and word.startswith("~") else word for word in arguments]
            read_git(handed, directory.cwd, self.reading)
        elif program == "gh":
            read_gh(arguments, directory, self.reading)
        elif wrapper in command_wrappers.COMMAND_WRAPPERS and wrapper not in SHELL_WRAPPERS:
            wrapped = wrapped_command(wrapper, arguments)
            if wrapped is not None and wrapped.words and wrapped.words[0] is not None:
                if wrapped.assignments and command_name(wrapped.words[0]) in GIT_NAMES:
                    names = ", ".join(name for name, _ in wrapped.assignments if git_reads(name))
                    if names:
                        self.reading.cannot_read(f"an assignment before git, {names}, which git reads")
                self.read_native(wrapped.words, Directory(None) if wrapped.moved else directory)
        elif self.depth >= NESTING_LIMIT:
            return
        elif program in command_wrappers.SCRIPT_SHELLS:
            script = shell_script(arguments)
            if script is not None:
                read_bash(script, directory.cwd, self.reading, self.depth + 1)
        elif program in POWERSHELL_PROGRAMS:
            read_powershell_start(program, arguments, directory.cwd, self.reading, self.depth + 1)

    def read_cmdlet(self, cmdlet: str, arguments: list[Word], directory: Directory) -> Outcome:
        """A command POWERSHELL_COMMANDS names: one that moves the location is followed, and any other moves
        nothing."""
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

    def read_location(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """Set-Location or Push-Location to a literal file-system path, by -Path, -LiteralPath, or position; a leading
        ~ is home; - and +, a path on another provider, and any other parameter leave the location unknown, as a path
        the text does not settle does."""
        if arguments and arguments[0] is not None and arguments[0].lower() in {"-path", "-literalpath"}:
            arguments = arguments[1:]
        stack = (directory.cwd, *directory.stack) if push else directory.stack
        start = home(self.reading.environment)
        if not arguments:
            return certain(Directory(None, stack)) if start is None else moved_to(directory, start, stack)
        target = arguments[0]
        if target is not None and target.startswith("-") and target != "-":
            return certain(UNKNOWN)
        if len(arguments) > 1 or target is None or target in {"-", "+"} or PROVIDER_PATH.match(target):
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
    # Whether the hubs of the places the command's git calls act on are being looked up: a lookup the reading time cuts
    # short, or one git answers with an error for, leaves a place not looked up, which may be a guarded hub's.
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
            looking_up = False
            reason = denied(reading, hubs)
            refused = None if reason is None else f"{reason} {DENY_REASON}"
            for note in reading.notes:
                print(f"hub guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not block every tool; the regression suite pins what it refuses
        cut_short = looking_up and isinstance(exc, (ReadingTimeout, GitError))
        if in_view is None or not (in_view[1] or cut_short):
            print(f"hub guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        # Where a guarded hub is in view, a command the guard failed to read or judge is refused, whether a reader fails
        # on nesting past Python's recursion limit or on any other error: a guard that fails may have missed a git call
        # it reads, which is unlike a part of the command it does not read.
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

"""Holds tools/hub_guard.py's option allow-lists to each runner's and program's real option catalogue: while a hub is
guarded, every option word of a program RUNNER_OPTIONS or PROGRAM_OPTIONS names is refused unless it is one this
suite names as safe, in every spelling getopt reads, and a runner whose command is or may be git is refused whatever
its options.

The rows come from tests/fixtures/hub-guard/option-catalogues.json, each program's options as its --help or its getopt
table lists them, for the programs the Rust coreutils (uutils) replace, as ubuntu-26.04 does, from
tests/fixtures/hub-guard/option-catalogues-uutils.json too, and for the runners macOS installs, Apple's builds of
FreeBSD's, from tests/fixtures/hub-guard/option-catalogues-bsd.json, never from the guard's tables: every option any
implementation lists, alone, with its value attached and after it, abbreviated where the program reads an
abbreviation, and bundled with each other short option. Which options are safe is SAFE below, a literal of this
suite's own, and the guard's tables are held to it. Where the system installs a runner, Git Bash's on Windows and the
system's elsewhere, the options it lists are read too, from its --help, or, for a BSD build, which has none, from the
usage synopsis it prints for an option it does not take, and every one must be in the catalogue of its
implementation: the one its --version names, or on macOS, where the system's builds name none, the BSD one. So a
release of any of them that adds an option fails here until the fixture and SAFE decide it.
"""

from __future__ import annotations

import functools
import itertools
import json
import re
import shlex
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import option_reader
from command_wrappers import FIND_COMMANDS
from guard_limits import process_limits_held

from deployer import platform_support
from tools import hub_guard

SKIP_ALLOWED = {
    "CatalogueTests.lists_every_option": "a program the system does not install has no --help to read",
}

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hub-guard"
CATALOGUES = FIXTURES / "option-catalogues.json"
# The Rust coreutils' builds of the programs they replace, catalogued beside the GNU builds, so the rows hold the guard
# to every option either takes.
RUST_CATALOGUES = FIXTURES / "option-catalogues-uutils.json"
# macOS's builds of the runners it installs, Apple's builds of FreeBSD's, catalogued from Apple's source.
BSD_CATALOGUES = FIXTURES / "option-catalogues-bsd.json"
# Each implementation a program's --version names in parentheses after its name, with its catalogue: GNU's prints
# `env (GNU coreutils) 8.32`, and the Rust coreutils' `env (uutils coreutils) 0.10.0`, by uucore's crate_version. A BSD
# build takes no --version, so it names none.
IMPLEMENTATIONS = {"GNU": CATALOGUES, "uutils": RUST_CATALOGUES, "BSD": BSD_CATALOGUES}
VERSION_LINE = re.compile(r"(\S+) \((GNU|uutils) [a-z]+\) ")
# The options this suite holds safe: they neither run a command nor build one from text, change where one runs,
# write a file the command does not name, nor change what the guard reads.
SAFE: dict[str, set[str]] = {
    "env": {"-i", "--ignore-environment", "-0", "--null", "-u", "--unset", "-v", "--debug"},
    "xargs": {
        *("-0", "--null", "-a", "--arg-file", "-d", "--delimiter", "-E", "-L", "-n", "--max-args", "-P"),
        *("--max-procs", "-r", "--no-run-if-empty", "-s", "--max-chars", "-t", "--verbose", "-x", "--exit"),
    },
    "timeout": {
        *("-k", "--kill-after", "-s", "--signal", "-v", "--verbose", "-p", "--preserve-status", "-f"),
        "--foreground",
    },
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
    "chroot": set(),
    "ionice": {"-c", "--class", "-n", "--classdata", "-t", "--ignore"},
    "chrt": {
        *("-b", "--batch", "-d", "--deadline", "-f", "--fifo", "-i", "--idle", "-o", "--other", "-r", "--rr"),
        *("-R", "--reset-on-fork", "-v", "--verbose"),
    },
    "taskset": {"-c", "--cpu-list"},
    "setsid": {"-c", "--ctty", "-f", "--fork", "-w", "--wait"},
    "unbuffer": {"-p"},
    "chronic": {"-e", "-v"},
    "strace": {"-c", "-f", "-q"},
    "ltrace": {"-c", "-f"},
    "time": {"-f", "--format", "-p", "--portability", "-q", "--quiet", "-v", "--verbose"},
    "sudo": {
        *("-n", "--non-interactive", "-u", "--user", "-g", "--group", "-H", "--set-home", "-E", "-k"),
        "--reset-timestamp",
    },
    "doas": {"-n", "-u"},
    "find": {
        *("-H", "-L", "-P", "-not", "-a", "-and", "-o", "-or", "-daystart", "-follow", "-nowarn", "-regextype"),
        *("-warn", "-depth", "-files0-from", "-maxdepth", "-mindepth", "-mount", "-noleaf", "-xdev"),
        *("-ignore_readdir_race", "-noignore_readdir_race", "-amin", "-anewer", "-atime", "-cmin", "-cnewer"),
        *("-context", "-ctime", "-empty", "-false", "-fstype", "-gid", "-group", "-ilname", "-iname", "-inum"),
        *("-ipath", "-iwholename", "-iregex", "-links", "-lname", "-mmin", "-mtime", "-name", "-newer"),
        *(f"-newer{x}{y}" for x in "aBcm" for y in "aBcmt"),
        *("-nouser", "-nogroup", "-path", "-perm", "-regex", "-readable", "-writable"),
        *("-executable", "-samefile", "-wholename", "-size", "-true", "-type", "-uid", "-used", "-user"),
        *("-xtype", "-print0", "-printf", "-print", "-ls", "-prune", "-quit"),
    },
    "rg": {
        *("-A", "--after-context", "-B", "--before-context", "-C", "--context", "-c", "--count"),
        *("--count-matches", "-s", "--case-sensitive", "--color", "--colors", "--column", "--no-column"),
        *("--context-separator", "-e", "--regexp", "-E", "--encoding", "-f", "--file", "-F", "--fixed-strings"),
        *("--files", "-l", "--files-with-matches", "--files-without-match", "-L", "--follow", "-g", "--glob"),
        *("--iglob", "--glob-case-insensitive", "-.", "--hidden", "--no-hidden", "--heading", "--no-heading"),
        *("-i", "--ignore-case", "--ignore-file", "--json", "-n", "--line-number", "-N", "--no-line-number"),
        *("-x", "--line-regexp", "-M", "--max-columns", "-m", "--max-count", "-d", "--max-depth"),
        *("--max-filesize", "-U", "--multiline", "--multiline-dotall", "--no-config", "--no-ignore"),
        *("--no-ignore-vcs", "--no-ignore-dot", "--no-ignore-global", "--no-ignore-parent", "--no-ignore-exclude"),
        *("--no-ignore-files", "--no-messages", "-0", "--null", "--null-data", "-o", "--only-matching"),
        *("--passthru", "-P", "--pcre2", "-p", "--pretty", "-q", "--quiet", "-r", "--replace", "-S"),
        *("--smart-case", "--sort", "--sortr", "--stats", "-a", "--text", "-j", "--threads", "--trim", "-t"),
        *("--type", "-T", "--type-not", "--type-list", "-u", "--unrestricted", "--vimgrep", "-H"),
        *("--with-filename", "-I", "--no-filename", "-w", "--word-regexp", "-v", "--invert-match", "-b"),
        *("--byte-offset", "--binary", "--no-binary", "--crlf", "-V"),
    },
    "sort": {
        *("-b", "--ignore-leading-blanks", "-d", "--dictionary-order", "-f", "--ignore-case", "-g"),
        *("--general-numeric-sort", "-i", "--ignore-nonprinting", "-M", "--month-sort", "-h"),
        *("--human-numeric-sort", "-n", "--numeric-sort", "-R", "--random-sort", "--random-source", "-r"),
        *("--reverse", "--sort", "-V", "--version-sort", "--batch-size", "-c", "--check", "-C", "--debug"),
        *("--files0-from", "-k", "--key", "-m", "--merge", "-s", "--stable", "-S", "--buffer-size", "-t"),
        *("--field-separator", "--parallel", "-u", "--unique", "-z", "--zero-terminated", "--check-silent"),
    },
    "tar": {
        *("-A", "--catenate", "--concatenate", "-c", "--create", "--delete", "-d", "--diff", "--compare", "-r"),
        *("--append", "-t", "--list", "-u", "--update", "--ignore-failed-read", "--force-local"),
        *("--no-seek", "-n", "--seek", "-S", "--sparse", "--exclude", "--exclude-backups"),
        *("--exclude-caches", "--exclude-caches-all", "--exclude-caches-under", "--exclude-tag"),
        *("--exclude-tag-all", "--exclude-tag-under", "--exclude-vcs", "--exclude-vcs-ignores", "--no-null"),
        *("--no-recursion", "--no-unquote", "--no-verbatim-files-from", "--null", "--recursion"),
        *("--unquote", "--verbatim-files-from", "-X", "--exclude-from", "--anchored"),
        *("--ignore-case", "--no-anchored", "--no-ignore-case", "--no-wildcards", "--no-wildcards-match-slash"),
        *("--wildcards", "--wildcards-match-slash", "--keep-newer-files", "-k", "--keep-old-files"),
        *("--no-overwrite-dir", "--skip-old-files"),
        *("--verify", "-O", "--to-stdout", "--clamp-mtime", "--delay-directory-restore", "--group"),
        *("--mode", "--mtime", "-m", "--touch", "--no-delay-directory-restore", "--no-same-owner"),
        *("--no-same-permissions", "--numeric-owner", "--owner", "-p", "--preserve-permissions"),
        *("--same-permissions", "--same-owner", "--sort", "--acls", "--no-acls", "--no-selinux"),
        *("--no-xattrs", "--selinux", "--xattrs", "-f", "--file", "-b", "--blocking-factor", "-B"),
        *("--read-full-records", "-i", "--ignore-zeros", "--record-size", "--format", "--old-archive"),
        *("--portability", "--posix", "-V", "--label", "-a", "--auto-compress", "-j", "--bzip2", "-J", "--xz"),
        *("--lzip", "--lzma", "--lzop", "--no-auto-compress", "--zstd", "-z", "--gzip", "--gunzip", "--ungzip"),
        *("-Z", "--compress", "--uncompress", "--hard-dereference", "-h", "--dereference", "-K", "--newer-mtime"),
        *("-N", "--newer", "--after-date", "--one-file-system", "-P", "--absolute-names", "--strip-components"),
        *("--transform", "--xform", "--full-time", "-l", "--check-links", "--quoting-style", "-R"),
        *("--block-number", "--show-omitted-dirs", "--show-transformed-names", "--show-stored-names", "--utc"),
        *("-v", "--verbose", "--warning", "-o", "--starting-file"),
    },
}
# Another build of a program the guard reads by one table, whose catalogue the fixture holds beside it.
OTHER_BUILDS = {"bsdtar": "tar"}
# The Windows builds, in System32, of programs the guard reads, whose options are written with a slash, and the slash
# options of each this suite holds safe: Windows' sort writes a file by /O and keeps its working files by /T.
WINDOWS_BUILDS = {"sort.exe": "sort", "find.exe": "find"}
SAFE_SLASH = {
    "sort": {"/+n", "/L", "/LOCALE", "/M", "/MEMORY", "/REC", "/RECORD_MAXIMUM", "/R", "/REVERSE"},
    "find": {"/V", "/C", "/N", "/I", "/OFF", "/OFFLINE"},
}
# The words each runner takes after its options and before its command, as its manual gives them.
OPERANDS = {"timeout": ["5"], "chrt": ["10"], "taskset": ["3"], "chroot": ["/srv"]}
# Bash reads time as a keyword at a command's start, so the program time runs only after an assignment.
BEFORE = {"time": "x=1 "}
# Forms no catalogue lists, each with whether it is understood before a command that is not git.
EXTRA_FORMS: dict[str, list[tuple[list[str], bool]]] = {
    "env": [(["-"], True), (["X=1"], True), (["-i", "X=1", "-"], True), (["--"], True), (["-u", "X", "Y=1"], True)],
    "nice": [(["-5"], True), (["--5"], True), (["-n5"], True), (["-n", "5", "-n", "3"], True), (["-5x"], False)],
    "timeout": [(["-vk5"], True), (["--sig=TERM"], False), (["--"], True), (["-vI5"], False)],
    "nohup": [(["--"], True)],
    "xargs": [(["-0rn1"], True), (["-0IX"], False), (["-I{}"], False), (["--replac=X"], False)],
    "tar": [
        (["tf", "a.tar"], True),
        (["czf", "a.tar", "d"], True),
        # Extraction writes the files an archive names, and -C chooses where.
        (["xf", "a.tar"], False),
        (["-xf", "a.tar"], False),
        (["-tf", "a.tar", "-C", "d"], False),
        # An archive named host:path is opened through a remote shell, unless --force-local makes it a file.
        (["-tf", "host:a.tar"], False),
        (["-tfhost:a.tar"], False),
        (["tf", "host:a.tar"], False),
        (["--file=host:a.tar", "-t"], False),
        (["--file", "host:a.tar", "-t"], False),
        (["--force-local", "-tf", "host:a.tar"], True),
        # --force-local only as an option: not as another option's value, nor as a word after --.
        (["-tf", "host:a.tar", "--", "--force-local"], False),
        (["-tf", "host:a.tar", "--exclude", "--force-local"], False),
        (["-cf", "host:a.tar", "-T", "--force-local"], False),
        # Letters bsdtar, Windows' tar.exe, reads with a value where GNU tar reads none, or the other way.
        (["-tf", "a.tar", "-H", "--use-compress-program=git"], False),
        (["-tf", "a.tar", "-W", "use-compress-program=git"], False),
        (["-tf", "./a:b.tar"], True),
        # A value given to an option that takes none.
        (["--verbose=1", "-tf", "a.tar"], False),
        (["--verbose", "-tf", "a.tar"], True),
        # A list of names, which tar reads for options too unless --verbatim-files-from is given.
        (["-cf", "a.tar", "-T", "list"], False),
        (["-cf", "a.tar", "--files-from=list"], False),
        (["xIf", "a.tar"], False),
        (["-xIgit", "-f", "a.tar"], False),
        (["--to-com=git", "-xf", "a.tar"], False),
    ],
    "sort": [(["--compress=x"], False), (["-uk2"], True), (["-ok"], False)],
    "find": [(["-O3"], False), (["-newer", "x", "-exec", "ls", "{}", ";"], False), (["-name=x"], False)],
    "rg": [(["-uuu"], True), (["-g*.py"], True), (["--pre", "x"], False), (["--count=1"], False), (["--count"], True)],
    # sudo sets a variable a NAME=VALUE word before its command names.
    "sudo": [(["X=1"], False), (["-u", "root", "X=1"], False), (["-u", "root"], True)],
    "doas": [(["X=1"], False)],
}
VALUE = "1"
HELP_OPTION = re.compile(r"^\s+(?:(-[A-Za-z0-9]),?\s*)?(?:(--[a-z][a-z0-9-]*)(\[=|=)?)?")
# The runners whose --help this suite reads where the system installs them.
CHECKED = ("env", "nice", "nohup", "stdbuf", "timeout", "xargs")
# Each runner's --help and --version, and what it prints for -@, an option no implementation takes, which is where a
# BSD build prints its usage synopsis: its nice and nohup run --help as a program. A BSD build refuses each with a
# failing status, and what it printed is still read.
DISCOVER = r"""cd "$1" || exit 1
shift
for name in "$@"; do
  path=$(type -P "$name") || continue
  printf '%s\n' "$path" > "path.$name"
  "$path" --help < /dev/null > "help.$name" 2>&1 || :
  "$path" --version < /dev/null > "version.$name" 2>&1 || :
  "$path" -@ < /dev/null > "usage.$name" 2>&1 || :
done"""
# A usage synopsis's options: a bracket that opens on one option, or on options joined by |, each of one letter or of
# several letters that take no value, and the value word that follows it, when it takes one.
USAGE_OPTION = re.compile(r"\[(-[A-Za-z0-9]+(?:\|-[A-Za-z0-9]+)*)(\s+[^\s\[\]|-][^\s\[\]|]*)?")
WORKSPACE = tempfile.TemporaryDirectory(prefix="runner-forms.")


@dataclass(frozen=True)
class Option:
    short: str | None
    long: str | None
    value: str  # "none", "value", or "optional"


@dataclass(frozen=True)
class Form:
    words: tuple[str, ...]
    names: frozenset[str]  # the options it spells
    abbreviated: bool = False


@functools.cache
def catalogue(path: Path) -> dict[str, dict[str, object]]:
    loaded: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def catalogues() -> dict[str, dict[str, object]]:
    return catalogue(CATALOGUES)


def listed_options(path: Path, program: str) -> list[Option]:
    """The options one catalogue lists for a program."""
    entries = catalogue(path)[program]["options"]
    assert isinstance(entries, list)
    return [Option(entry["short"], entry["long"], entry["value"]) for entry in entries]


def options_of(program: str) -> list[Option]:
    """Every option any implementation's catalogue lists for a program, so the guard is held to each of them."""
    found = [
        option
        for path in IMPLEMENTATIONS.values()
        if program in catalogue(path)
        for option in listed_options(path, program)
    ]
    return list(dict.fromkeys(found))


def names(options: list[Option]) -> set[str]:
    return {each for option in options for each in (option.short, option.long) if each}


def implementation(version: str) -> str | None:
    """The implementation a program's --version names on its first line, GNU or uutils; None for any other."""
    lines = version.strip().splitlines()
    match = VERSION_LINE.match(lines[0]) if lines else None
    return match[2] if match else None


def installed_implementation(version: str) -> str | None:
    """The implementation of a runner the system installs: the one its --version names, or on macOS, whose own runners
    are Apple's builds of FreeBSD's and name none, BSD."""
    return implementation(version) or ("BSD" if platform_support.MACOS else None)


def usage_options(text: str, program: str) -> tuple[Option, ...]:
    """The options a BSD build's usage synopsis lists, from its `usage: <program>` line and the indented lines after
    it, each with whether it takes a value: `[-0iv]` three that take none, `[-n number]` one that takes one, and
    `[-L|-U user]` two that each take one."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(f"usage: {program} ")), None)
    if start is None:
        return ()
    synopsis = [lines[start]]
    synopsis += itertools.takewhile(lambda line: line[:1].isspace(), lines[start + 1 :])
    found: list[Option] = []
    for match in USAGE_OPTION.finditer(" ".join(synopsis)):
        valued = match.group(2) is not None
        for word in match.group(1).split("|"):
            if valued and len(word) == 2:
                found.append(Option(word, None, "value"))
            else:
                found.extend(Option(f"-{letter}", None, "none") for letter in word[1:])
    return tuple(dict.fromkeys(found))


def spelled(name: str, kind: str, attach: str) -> list[tuple[str, ...]]:
    """One option name's spellings: alone, or with its value after it or attached, a short one directly and a long one
    by `=`."""
    if kind == "none":
        return [(name,)]
    attached = (name + attach + VALUE,)
    return [(name, VALUE), attached] if kind == "value" else [(name,), attached]


def option_forms(program: str, option: Option) -> list[Form]:
    """Each way one option can be written, its long name also abbreviated to every prefix of three or more characters
    that is no other option's full name, where the program reads abbreviations."""
    forms: list[Form] = []
    if option.short is not None:
        forms += [Form(words, frozenset({option.short})) for words in spelled(option.short, option.value, "")]
    if option.long is not None and program == "find":
        # find reads each of its words exactly, its value always the next word.
        exact: tuple[str, ...] = (option.long, VALUE) if option.value == "value" else (option.long,)
        forms.append(Form(exact, frozenset({option.long})))
    elif option.long is not None:
        forms += [Form(words, frozenset({option.long})) for words in spelled(option.long, option.value, "=")]
        if catalogues()[program]["abbreviates"]:
            names = {each.long for each in options_of(program)}
            for end in range(3, len(option.long)):
                prefix = option.long[:end]
                if prefix in names:
                    continue
                for words in spelled(prefix, option.value, "="):
                    forms.append(Form(words, frozenset({option.long}), abbreviated=True))
    return forms


def bundles(program: str) -> list[Form]:
    """Each short option that takes no value bundled with each other short option, the second's value attached and
    after the bundle; and, for tar, each short option's letter in an old-style first word after t."""
    shorts = [option for option in options_of(program) if option.short is not None]
    forms: list[Form] = []
    for first in shorts:
        if first.value != "none" or first.short is None:
            continue
        for second in shorts:
            if second is first or second.short is None:
                continue
            names = frozenset({first.short, second.short})
            bundle = first.short + second.short[1:]
            forms += [Form(words, names) for words in spelled(bundle, second.value, "")]
    if program == "tar":
        for option in shorts:
            assert option.short is not None
            letters = "t" + option.short[1:]
            names = frozenset({"-t", option.short})
            forms.append(Form((letters, VALUE) if option.value == "value" else (letters,), names))
    return forms


def program_forms(program: str) -> list[Form]:
    forms = [form for option in options_of(program) for form in option_forms(program, option)]
    if program != "find":
        forms += bundles(program)
    return list(dict.fromkeys(forms))


def understood(form: Form, program: str) -> bool:
    """Whether this suite holds a form understood: every option it spells is safe and spelled in full."""
    return not form.abbreviated and form.names <= SAFE[program]


def refused(command: str) -> bool:
    reading = hub_guard.Reading({}, command)
    with process_limits_held():
        hub_guard.read_bash(command, Path.cwd(), reading)
    return bool(reading.unread)


def runner_command(name: str, words: tuple[str, ...] | list[str], command: str) -> str:
    return " ".join([BEFORE.get(name, "") + name, shlex.join(words), *OPERANDS.get(name, []), command]).replace(
        "  ", " "
    )


def program_command(name: str, words: tuple[str, ...] | list[str]) -> str:
    return f"find . {shlex.join(words)}" if name == "find" else f"{name} {shlex.join(words)} x"


def help_options(text: str) -> tuple[Option, ...]:
    """The options a program's --help lists, each with whether it takes a value."""
    found: list[Option] = []
    for line in text.splitlines():
        match = HELP_OPTION.match(line)
        if match is None or not (match.group(1) or match.group(2)):
            continue
        short, long, equals = match.groups()
        if long is None and short is not None and not re.match(r"^\s+-[A-Za-z0-9](?:,|\s{2}|\s*$)", line):
            continue
        value = {"[=": "optional", "=": "value"}.get(equals or "", "none")
        if value == "none" and short is not None and long is None:
            words = line.split()
            value = "value" if len(words) > 1 and words[1].isupper() else "none"
        found.append(Option(short, long, value))
    return tuple(dict.fromkeys(found))


def find_bash() -> str:
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash lists the runners' options the fixture is held to")
    return bash


@functools.cache
def installed_help() -> dict[str, tuple[str, str, str]]:
    """The --help, the --version, and the refusal of an unknown option of each runner CHECKED names that the system
    installs."""
    workspace = Path(WORKSPACE.name)
    result = platform_support.run_tool(
        [find_bash(), "-c", DISCOVER, "discover", platform_support.normalize(workspace), *CHECKED], timeout=120
    )
    if result.returncode != 0:
        raise AssertionError(f"Bash could not list the runners: {result.output.strip()}")
    return {
        name: (
            (workspace / f"help.{name}").read_text(encoding="utf-8", errors="replace"),
            (workspace / f"version.{name}").read_text(encoding="utf-8", errors="replace"),
            (workspace / f"usage.{name}").read_text(encoding="utf-8", errors="replace"),
        )
        for name in CHECKED
        if (workspace / f"path.{name}").is_file()
    }


class RunnerFormTests(unittest.TestCase):
    def holds(self, name: str) -> None:
        forms = program_forms(name)
        self.assertTrue(forms, f"{name} gave no forms")
        rows = [(list(form.words), understood(form, name)) for form in forms] + EXTRA_FORMS.get(name, [])
        for words, expected in rows:
            with self.subTest(form=shlex.join(words)):
                self.assertTrue(refused(runner_command(name, words, "git status")), "a runner before git is refused")
                self.assertEqual(not expected, refused(runner_command(name, words, "ls -la")))

    def test_env(self) -> None:
        self.holds("env")

    def test_xargs(self) -> None:
        self.holds("xargs")

    def test_timeout(self) -> None:
        self.holds("timeout")

    def test_nice(self) -> None:
        self.holds("nice")

    def test_nohup(self) -> None:
        self.holds("nohup")

    def test_stdbuf(self) -> None:
        self.holds("stdbuf")

    def test_chroot(self) -> None:
        self.holds("chroot")

    def test_ionice(self) -> None:
        self.holds("ionice")

    def test_chrt(self) -> None:
        self.holds("chrt")

    def test_taskset(self) -> None:
        self.holds("taskset")

    def test_setsid(self) -> None:
        self.holds("setsid")

    def test_unbuffer(self) -> None:
        self.holds("unbuffer")

    def test_chronic(self) -> None:
        self.holds("chronic")

    def test_strace(self) -> None:
        self.holds("strace")

    def test_ltrace(self) -> None:
        self.holds("ltrace")

    def test_time(self) -> None:
        self.holds("time")

    def test_sudo(self) -> None:
        self.holds("sudo")

    def test_doas(self) -> None:
        self.holds("doas")


# These hold each GNU build to its allow-list; WindowsBuildTests holds the Windows builds, where sort's dash words are
# also held to sort.exe's slash options.
@mock.patch.object(platform_support, "SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS", False)
class ProgramFormTests(unittest.TestCase):
    def holds(self, name: str) -> None:
        forms = program_forms(name)
        self.assertTrue(forms, f"{name} gave no forms")
        rows = [(list(form.words), understood(form, name)) for form in forms] + EXTRA_FORMS.get(name, [])
        for words, expected in rows:
            with self.subTest(form=shlex.join(words)):
                self.assertEqual(not expected, refused(program_command(name, words)))

    def test_find(self) -> None:
        self.holds("find")

    def test_rg(self) -> None:
        self.holds("rg")

    def test_sort(self) -> None:
        self.holds("sort")

    def test_tar(self) -> None:
        self.holds("tar")


@mock.patch.object(platform_support, "SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS", False)
class ProgramBehindRunnerTests(unittest.TestCase):
    def test_a_program_a_runner_starts_is_held_to_its_own_allow_list(self) -> None:
        # Every form of each program behind timeout, and a sample of each behind every other runner.
        for runner in sorted(hub_guard.RUNNER_OPTIONS):
            for program in sorted(hub_guard.PROGRAM_OPTIONS):
                forms = program_forms(program)
                if runner != "timeout":
                    forms = [form for form in forms if understood(form, program)][:5] + [
                        form for form in forms if not understood(form, program)
                    ][:5]
                for form in forms:
                    command = runner_command(runner, (), program_command(program, form.words))
                    # xargs adds words from its input, which may be any option, to a program with no `--` before them.
                    expected = runner == "xargs" or not understood(form, program)
                    with self.subTest(command=command):
                        self.assertEqual(expected, refused(command))

    @mock.patch.object(platform_support, "SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS", False)
    def test_after_xargs_a_program_whose_options_end_at_a_double_dash_is_held_to_its_allow_list(self) -> None:
        for command, expected in (
            ("xargs rg -n -- x", False),
            ("xargs sort -u --", False),
            ("xargs tar -cf a.tar --", False),
            ("xargs rg -e -- x", True),
            ("xargs rg --pre=git -- x", True),
            ("xargs find . -- -name x", True),
            ("xargs rg x", True),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, refused(command))


class WindowsBuildTests(unittest.TestCase):
    def rows(self, build: str) -> list[tuple[list[str], bool]]:
        """Each slash option of a Windows build, in upper and lower case, its value after it, with whether this suite
        holds it safe."""
        program = WINDOWS_BUILDS[build]
        rows: list[tuple[list[str], bool]] = []
        for option in options_of(build):
            for name in (option.short, option.long):
                if name is None:
                    continue
                spelling = "/+3" if name == "/+n" else name
                safe = name in SAFE_SLASH[program]
                rows.extend(
                    ([case, VALUE] if option.value == "value" else [case], safe)
                    for case in (spelling, spelling.lower())
                )
        return rows

    def test_each_windows_build_s_slash_option_is_refused_unless_safe_in_any_case(self) -> None:
        for build, program in WINDOWS_BUILDS.items():
            for words, safe in self.rows(build):
                with self.subTest(build=build, form=shlex.join(words)):
                    arguments: list[option_reader.Word] = [*words, "x"]
                    reason = hub_guard.program_refusal(program, arguments, windows_build=True)
                    self.assertEqual(not safe, reason is not None)
                    # The program's GNU build reads the same words as operands.
                    self.assertIsNone(hub_guard.program_refusal(program, arguments, windows_build=False))

    def test_on_windows_sort_and_find_are_read_as_the_windows_build_whatever_their_path(self) -> None:
        for flag in (True, False):
            with (
                self.subTest(flag=flag),
                mock.patch.object(platform_support, "SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS", flag),
            ):
                for command in (
                    "/c/Windows/System32/sort.exe /o out.txt f",
                    "C:/Windows/System32/sort.exe /T d f",
                    "'C:\\Windows\\System32\\sort.exe' /o f",
                    "'C:\\WINDOWS\\system32\\SORT.EXE' /O f",
                    "'C:\\Windows\\SysWOW64\\sort.exe' /o f",
                    "./sort.exe /o f",
                    "sort /o f",
                    "sort.exe -- /o out.txt f",
                    "timeout 5 /c/Windows/System32/sort.exe /o f",
                    "timeout 5 'C:\\Windows\\System32\\find.exe' /x y f",
                    "env 'C:\\WINDOWS\\SYSTEM32\\FIND.EXE' /x y f",
                    "echo x | xargs sort -u --",
                    # A path that starts with a slash is a word the Windows build may read as a slash option.
                    "find /tmp/ -name x",
                    "sort /c/x/f",
                    # A joined pair, an attached value, and a slash word another option takes as its value.
                    "sort /r/o out.txt f",
                    "sort /L C /O out.txt f",
                    "sort /L /O out.txt f",
                    "sort /Oout.txt f",
                    "sort /O:out.txt f",
                    "sort /Tdir f",
                    "sort -t /O out.txt f",
                    "find /c/v x f",
                    "sort -u f",
                    "sort -t x f",
                ):
                    with self.subTest(command=command):
                        self.assertEqual(flag, refused(command))
                # The Windows build's safe options, GNU sort's, and paths that start with no slash.
                for command in ("/c/Windows/System32/sort.exe /r f", "sort -r f", "find tmp/ -name x", "sort ./f"):
                    with self.subTest(command=command):
                        self.assertFalse(refused(command))
        self.assertEqual(platform_support.WINDOWS, platform_support.SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS)

    def test_a_slash_word_that_is_not_exactly_a_safe_option_is_refused_wherever_it_stands(self) -> None:
        # How sort.exe reads a joined pair, an attached value, or a slash word another option takes as its value is not
        # documented, so each is refused unless it is exactly one of SAFE_SLASH's options.
        for program, words, safe in (
            ("sort", "/r/o out.txt f", False),
            ("sort", "/R/R f", False),
            ("sort", "/r/+3 f", False),
            ("sort", "/L C /O out.txt f", False),
            ("sort", "/L /O out.txt f", False),
            ("sort", "/REC /T d f", False),
            ("sort", "/Oout.txt f", False),
            ("sort", "/O:out.txt f", False),
            ("sort", "/o:out.txt f", False),
            ("sort", "/Tdir f", False),
            ("sort", "/R:x f", False),
            ("sort", "-t /O out.txt f", False),
            ("sort", "-k /O out.txt f", False),
            ("sort", "-- /tmp/x", False),
            ("sort", "f /", False),
            # Python's upper() folds these onto /REVERSE and /RECORD_MAXIMUM; only ASCII case is folded.
            ("sort", "/reve\u017fe f", False),
            ("sort", "/record_max\u0131mum 10 f", False),
            ("find", "/\u0131 x f", False),
            ("find", "/c/v x f", False),
            ("find", "/Cx f", False),
            ("find", "-name /O x", False),
            ("sort", "/r f", True),
            ("sort", "/REVERSE /L C f", True),
            ("sort", "/l c /m 100 /rec 10 f", True),
            ("sort", "/+3 f", True),
            # sort.exe may read a dash as a slash, so a dash word passes only as a safe slash option too.
            ("sort", "-t x f", False),
            ("sort", "-temporary d f", False),
            ("sort", "-o out.txt f", False),
            ("sort", "-u f", False),
            ("sort", "--reverse f", False),
            ("sort", "- f", False),
            ("sort", "-r f", True),
            ("sort", "-R f", True),
            # find.exe has no option that writes or runs anything; GNU find's table judges its dash words.
            ("find", ". -name x", True),
            ("find", ". -type f -print", True),
            ("find", "/c /i /OFFLINE x f", True),
        ):
            with self.subTest(program=program, words=words):
                arguments: list[option_reader.Word] = list(words.split())
                reason = hub_guard.program_refusal(program, arguments, windows_build=True)
                self.assertEqual(not safe, reason is not None)

    def test_the_guard_s_slash_options_are_the_ones_this_suite_holds_safe(self) -> None:
        for build, program in WINDOWS_BUILDS.items():
            table = hub_guard.PROGRAM_OPTIONS[program].slash
            assert table is not None
            names = {"/+n" if name == "+" else f"/{name}" for name in table}
            with self.subTest(program=program):
                self.assertEqual(sorted(SAFE_SLASH[program]), sorted(names))
                listed = {each for option in options_of(build) for each in (option.short, option.long) if each}
                self.assertLessEqual(SAFE_SLASH[program], listed)
        self.assertEqual(
            sorted(WINDOWS_BUILDS.values()),
            sorted(name for name, table in hub_guard.PROGRAM_OPTIONS.items() if table.slash is not None),
        )


class CatalogueTests(unittest.TestCase):
    def test_every_program_the_guard_reads_options_of_has_a_catalogue_a_test_and_its_safe_options(self) -> None:
        tables = {**hub_guard.RUNNER_OPTIONS, **hub_guard.PROGRAM_OPTIONS}
        self.assertEqual(sorted(tables), sorted(set(catalogues()) - set(OTHER_BUILDS) - set(WINDOWS_BUILDS)))
        self.assertEqual(sorted(tables), sorted(SAFE))
        tested = {
            name.removeprefix("test_")
            for suite in (RunnerFormTests, ProgramFormTests)
            for name in dir(suite)
            if name.startswith("test_")
        }
        self.assertEqual(sorted(tables), sorted(tested))
        for name, table in tables.items():
            with self.subTest(program=name):
                prefix = "-" if table.single_dash else "--"
                allowed = {f"-{letter}" for letter in table.short} | set(table.long)
                self.assertEqual(sorted(SAFE[name]), sorted(allowed))
                self.assertTrue(all(each.startswith(prefix) for each in table.long))
                self.assertLessEqual(SAFE[name], names(options_of(name)), "a safe option the program does not list")

    def test_every_runner_but_the_shells_and_the_ones_always_refused_reads_its_options(self) -> None:
        readers = set(hub_guard.RUNNERS) - hub_guard.RUNNERS_ALWAYS_REFUSED - {"bash", "pwsh"}
        self.assertEqual(sorted(readers), sorted(hub_guard.RUNNER_OPTIONS))
        self.assertEqual(set(), set(hub_guard.PROGRAM_OPTIONS) & set(hub_guard.RUNNERS))

    def test_tar_s_safe_options_take_their_values_as_every_build_of_tar_reads_them(self) -> None:
        # From PowerShell on Windows tar is bsdtar, which reads some of GNU tar's letters with a value where GNU tar
        # reads none, or the other way, so a value word could escape the reading.
        for build, program in OTHER_BUILDS.items():
            kinds = {each: option.value for option in options_of(build) for each in (option.short, option.long) if each}
            gnu = {each: option.value for option in options_of(program) for each in (option.short, option.long) if each}
            for name in sorted(SAFE[program]):
                if name in kinds:
                    with self.subTest(build=build, option=name):
                        self.assertEqual(gnu[name], kinds[name])

    def test_find_s_actions_that_run_a_program_are_never_safe(self) -> None:
        self.assertEqual({"-exec", "-execdir", "-ok", "-okdir"}, FIND_COMMANDS)
        self.assertEqual(set(), FIND_COMMANDS & SAFE["find"])

    def test_the_rust_coreutils_catalogue_holds_programs_the_guard_reads_each_with_its_provenance(self) -> None:
        tables = {**hub_guard.RUNNER_OPTIONS, **hub_guard.PROGRAM_OPTIONS}
        rust = catalogue(RUST_CATALOGUES)
        # The programs the guard reads options of that the Rust coreutils replace: coreutils' runners, and sort.
        self.assertEqual(["chroot", "env", "nice", "nohup", "sort", "stdbuf", "timeout"], sorted(rust))
        self.assertLessEqual(set(rust), set(tables))
        for path in IMPLEMENTATIONS.values():
            for name, entry in catalogue(path).items():
                with self.subTest(catalogue=path.name, program=name):
                    self.assertTrue(str(entry["source"]).strip())
        for name, entry in rust.items():
            with self.subTest(program=name):
                self.assertTrue(str(entry["source"]).startswith("Rust coreutils (uutils) "))
                self.assertIn(f"src/uu/{name}/src/{name}.rs", str(entry["how"]))
                # option_forms abbreviates by the GNU catalogue's word, so both must say the same.
                self.assertEqual(catalogues()[name]["abbreviates"], entry["abbreviates"])

    def test_the_bsd_catalogue_holds_the_runners_macos_installs_each_with_its_provenance(self) -> None:
        tables = {**hub_guard.RUNNER_OPTIONS, **hub_guard.PROGRAM_OPTIONS}
        bsd = catalogue(BSD_CATALOGUES)
        # The runners CHECKED names that macOS installs: it has no stdbuf or timeout.
        self.assertEqual(["env", "nice", "nohup", "xargs"], sorted(bsd))
        self.assertLessEqual(set(bsd), set(tables))
        for name, entry in bsd.items():
            with self.subTest(program=name):
                self.assertTrue(str(entry["source"]).startswith("Apple shell_cmds-"))
                self.assertIn(f"{name}/{name}.c at apple-oss-distributions/shell_cmds tag ", str(entry["how"]))
        # macOS's env takes -P, which chooses the path its command is looked up on, and its xargs -J, which builds the
        # command, where GNU's take neither; neither is safe, so the rows hold the guard to refusing every form of each.
        self.assertIn(Option("-P", None, "value"), listed_options(BSD_CATALOGUES, "env"))
        self.assertNotIn("-P", names(listed_options(CATALOGUES, "env")) | SAFE["env"])
        self.assertIn(Option("-J", None, "value"), listed_options(BSD_CATALOGUES, "xargs"))
        self.assertNotIn("-J", names(listed_options(CATALOGUES, "xargs")) | SAFE["xargs"])

    def test_a_runner_the_system_installs_is_read_as_the_build_its_version_names_or_on_macos_bsd(self) -> None:
        refused = "env: illegal option -- -\nusage: env [-0iv] [-C workdir] [-P utilpath] [-S string]\n"
        for macos, version, expected in (
            (False, "env (GNU coreutils) 9.5\n", "GNU"),
            (True, "env (GNU coreutils) 9.5\n", "GNU"),
            (True, "env (uutils coreutils) 0.10.0\n", "uutils"),
            (True, refused, "BSD"),
            (True, "nice: --version: No such file or directory\n", "BSD"),
            (False, refused, None),
            (False, "env (BusyBox) v1.36\n", None),
        ):
            with self.subTest(macos=macos, version=version), mock.patch.object(platform_support, "MACOS", macos):
                self.assertEqual(expected, installed_implementation(version))

    def test_each_safe_option_takes_its_value_as_every_implementation_reads_it(self) -> None:
        # One read with a value by one implementation and without by another would let a value word escape the reading.
        for name in sorted(SAFE):
            kinds: dict[str, set[str]] = {}
            for path in IMPLEMENTATIONS.values():
                if name not in catalogue(path):
                    continue
                for option in listed_options(path, name):
                    for each in (option.short, option.long):
                        if each in SAFE[name]:
                            kinds.setdefault(each, set()).add(option.value)
            for each, found in sorted(kinds.items()):
                with self.subTest(program=name, option=each):
                    self.assertEqual(1, len(found), f"{each} is read as taking {sorted(found)}")

    def test_the_rows_hold_every_option_either_implementation_lists(self) -> None:
        for path in IMPLEMENTATIONS.values():
            for name in catalogue(path):
                with self.subTest(catalogue=path.name, program=name):
                    self.assertLessEqual(set(listed_options(path, name)), set(options_of(name)))
        env = [form.words for form in program_forms("env")]
        for words in (("-a", VALUE), ("--argv0", VALUE), ("-a" + VALUE,), ("--file=" + VALUE,), ("-h",), ("-V",)):
            with self.subTest(words=words):
                self.assertIn(words, env)
        self.assertIn(("-p",), [form.words for form in program_forms("timeout")])
        self.assertIn(("--check-silent",), [form.words for form in program_forms("sort")])

    def test_the_implementation_is_the_one_the_program_s_version_names(self) -> None:
        for version, expected in (
            ("env (GNU coreutils) 8.32\nCopyright (C) 2020 Free Software Foundation, Inc.\n", "GNU"),
            ("xargs (GNU findutils) 4.10.0\n", "GNU"),
            ("env (uutils coreutils) 0.10.0\n", "uutils"),
            ("\n  nice (uutils coreutils) 0.8.0\n", "uutils"),
            ("env 1.0\n", None),
            ("env (BusyBox) v1.36\n", None),
            ("", None),
        ):
            with self.subTest(version=version):
                self.assertEqual(expected, implementation(version))

    def lists_every_option(self, name: str) -> None:
        helps = installed_help()
        if name not in helps:
            self.skipTest(f"{name} is not installed here")
        text, version, usage = helps[name]
        first = (version.strip().splitlines() or [""])[0]
        build = installed_implementation(version)
        self.assertIsNotNone(build, f"{name}'s --version names neither GNU nor uutils: {first!r}")
        assert build is not None
        path = IMPLEMENTATIONS[build]
        self.assertIn(name, catalogue(path), f"{path.name} has no catalogue of {name}, which is {first!r} here")
        listed = names(listed_options(path, name))
        options = usage_options(usage, name) if build == "BSD" else help_options(text)
        if build == "BSD" and catalogue(path)[name]["options"]:
            # A synopsis the reader finds nothing in would hold the catalogue to nothing.
            self.assertTrue(options, f"{name} printed no usage synopsis this suite reads: {usage.strip()!r}")
        for option in options:
            for each in (option.short, option.long):
                if each is not None:
                    with self.subTest(option=each):
                        self.assertIn(each, listed, f"{first} lists an option {path.name} does not")

    def test_the_installed_runners_list_no_option_the_fixture_leaves_out(self) -> None:
        for name in CHECKED:
            with self.subTest(program=name):
                self.lists_every_option(name)


class UsageOptionsTests(unittest.TestCase):
    # Apple's synopses, as each usage() in shell_cmds-329 prints them.
    def test_reads_each_option_of_an_apple_synopsis_with_its_value_kind(self) -> None:
        env = (
            "env: illegal option -- @\n"
            "usage: env [-0iv] [-C workdir] [-P utilpath] [-S string]\n"
            "           [-u name] [name=value ...] [utility [argument ...]]\n"
        )
        self.assertEqual(
            (
                Option("-0", None, "none"),
                Option("-i", None, "none"),
                Option("-v", None, "none"),
                Option("-C", None, "value"),
                Option("-P", None, "value"),
                Option("-S", None, "value"),
                Option("-u", None, "value"),
            ),
            usage_options(env, "env"),
        )
        xargs = (
            "xargs: illegal option -- @\n"
            "usage: xargs [-0opt] [-E eofstr] [-I replstr [-R replacements] [-S replsize]]\n"
            "             [-J replstr] [-L number] [-n number [-x]] [-P maxprocs]\n"
            "             [-s size] [utility [argument ...]]\n"
        )
        self.assertEqual(
            {"-0", "-o", "-p", "-t", "-E", "-I", "-R", "-S", "-J", "-L", "-n", "-x", "-P", "-s"},
            {option.short for option in usage_options(xargs, "xargs")},
        )
        self.assertIn(Option("-x", None, "none"), usage_options(xargs, "xargs"))
        self.assertIn(Option("-R", None, "value"), usage_options(xargs, "xargs"))
        self.assertEqual(
            (Option("-n", None, "value"),),
            usage_options("nice: illegal option -- @\nusage: nice [-n increment] utility [argument ...]\n", "nice"),
        )
        self.assertEqual((), usage_options("usage: nohup [--] utility [arguments]\n", "nohup"))

    def test_reads_options_joined_by_a_bar_as_each_taking_the_value(self) -> None:
        self.assertEqual(
            (Option("-L", None, "value"), Option("-U", None, "value"), Option("-P", None, "value")),
            usage_options("usage: env [-L|-U user[/class]] [-P utilpath]\n", "env"),
        )

    def test_reads_nothing_but_the_program_s_own_synopsis(self) -> None:
        # GNU's refusal names no synopsis, and a line that is not indented ends the synopsis.
        self.assertEqual((), usage_options("env: invalid option -- '@'\nTry 'env --help'\n", "env"))
        self.assertEqual((), usage_options("usage: nice [-n increment] utility\n", "env"))
        self.assertEqual(
            (Option("-i", None, "none"),),
            usage_options("usage: env [-i] utility\nother: [-q] x\n", "env"),
        )


class HelpOptionsTests(unittest.TestCase):
    def test_reads_short_long_and_value_kinds(self) -> None:
        text = (
            "Usage: env [OPTION]...\n"
            "  -i, --ignore-environment  start with an empty environment\n"
            "  -C, --chdir=DIR       change working directory to DIR\n"
            "      --block-signal[=SIG]    block delivery of SIG signal(s) to COMMAND\n"
            "      --help     display this help and exit\n"
        )
        self.assertEqual(
            (
                Option("-i", "--ignore-environment", "none"),
                Option("-C", "--chdir", "value"),
                Option(None, "--block-signal", "optional"),
                Option(None, "--help", "none"),
            ),
            help_options(text),
        )


class FormTests(unittest.TestCase):
    def test_an_abbreviation_that_is_another_option_s_full_name_is_left_to_that_option(self) -> None:
        forms = [
            form.words for form in option_forms("tar", Option(None, "--exclude-caches-all", "none")) if form.abbreviated
        ]
        self.assertNotIn(("--exclude-caches",), forms)
        self.assertNotIn(("--exclude",), forms)
        self.assertIn(("--exclude-cache",), forms)

    def test_a_program_that_reads_no_abbreviation_gets_none(self) -> None:
        self.assertEqual([], [form for form in program_forms("rg") if form.abbreviated])
        self.assertTrue([form for form in program_forms("tar") if form.abbreviated])

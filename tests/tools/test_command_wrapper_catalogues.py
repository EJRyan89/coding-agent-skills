"""Holds ejryan89-skill-core's COMMAND_WRAPPERS to each wrapper program's real option catalogue: command_wrappers.py
places a wrapper's program past every option its builds take, so each row must hold every option the GNU and Rust
coreutils builds list, each taking its value as they take it, and read a dash word as macOS's getopt-only build does.

The rows come from tests/fixtures/option-catalogues/option-catalogues.json, each program's options as its --help or its
getopt table lists them; for the programs the Rust coreutils (uutils) replace, as ubuntu-26.04 does, from
option-catalogues-uutils.json too; and for the wrappers macOS installs, Apple's builds of FreeBSD's, from
option-catalogues-bsd.json; never from the rows themselves. Where the system installs a wrapper, Git Bash's on Windows
and the system's elsewhere, the options it lists are read too, from its --help, or, for a BSD build, which has none,
from the usage synopsis it prints for an option it does not take, and every one must be in the catalogue of its
implementation: the one its --version names, or on macOS, where the system's builds name none, the BSD one. So a
release of any of them that adds an option fails here until the fixture and the row decide it.

The suite lives under tests/ rather than beside command_wrappers.py, since finding the system's Bash and naming its
platform go through deployer/platform_support.py, which ejryan89-skill-core never imports.
"""

from __future__ import annotations

import functools
import itertools
import json
import re
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import option_reader
from command_wrappers import COMMAND_WRAPPERS, UNREAD_PROGRAM, unwrapped

from deployer import platform_support

SKIP_ALLOWED = {
    "CatalogueTests.lists_every_option": "a program the system does not install has no --help to read",
}

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "option-catalogues"
CATALOGUES = FIXTURES / "option-catalogues.json"
# The Rust coreutils' builds of the wrappers they replace, catalogued beside the GNU builds, so each row is held to
# every option either takes.
RUST_CATALOGUES = FIXTURES / "option-catalogues-uutils.json"
# macOS's builds of the wrappers it installs, Apple's builds of FreeBSD's, catalogued from Apple's source.
BSD_CATALOGUES = FIXTURES / "option-catalogues-bsd.json"
# Each implementation a program's --version names in parentheses after its name, with its catalogue: GNU's prints
# `env (GNU coreutils) 8.32`, and the Rust coreutils' `env (uutils coreutils) 0.10.0`. A BSD build takes no --version.
IMPLEMENTATIONS = {"GNU": CATALOGUES, "uutils": RUST_CATALOGUES, "BSD": BSD_CATALOGUES}
VERSION_LINE = re.compile(r"(\S+) \((GNU|uutils) [a-z]+\) ")
# A getopt string's letters, each with whether it takes a value.
GETOPT_LETTER = re.compile(r"(.)(:?)")
# What FreeBSD's nice reads as a niceness: strtol's whole word, in base 10.
NICENESS = re.compile(r"\s*[+-]?[0-9]+")
HELP_OPTION = re.compile(r"^\s+(?:(-[A-Za-z0-9]),?\s*)?(?:(--[a-z][a-z0-9-]*)(\[=|=)?)?")
# The wrappers whose --help this suite reads where the system installs them.
CHECKED = ("env", "nice", "nohup", "stdbuf", "timeout", "xargs")
# The wrappers whose installed build this suite reads only where it is the one macOS installs: Apple's chroot and time,
# each held to its BSD catalogue. Their GNU builds, which Git Bash and Linux install, were catalogued from their own
# --help or getopt table and are held to the catalogue alone.
MACOS_CHECKED = ("chroot", "time")
# Each wrapper's --help and --version, and what it prints for -@, an option no implementation takes, which is where a
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
# A usage synopsis's options: a bracket that opens on one option, or on options joined by |, each a long option, one
# letter, or several letters that take no value, and the value word that follows it, when it takes one.
USAGE_OPTION = re.compile(r"\[\s*(--?[A-Za-z0-9][\w-]*(?:\s*\|\s*--?[A-Za-z0-9][\w-]*)*)(\s+[^\s\[\]|-][^\s\[\]|]*)?")
# A synopsis's placeholder for a value, capitals and a number, as sort's POS1.
PLACEHOLDER = re.compile(r"[A-Z]{2,}\d+")
WORKSPACE = tempfile.TemporaryDirectory(prefix="wrapper-catalogues.")


@dataclass(frozen=True)
class Option:
    short: str | None
    long: str | None
    value: str  # "none", "value", or "optional"


@functools.cache
def catalogue(path: Path) -> dict[str, dict[str, object]]:
    loaded: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def listed_options(path: Path, program: str) -> list[Option]:
    """The options one catalogue lists for a program."""
    entries = catalogue(path)[program]["options"]
    assert isinstance(entries, list)
    return [Option(entry["short"], entry["long"], entry["value"]) for entry in entries]


def names(options: list[Option]) -> set[str]:
    return {each for option in options for each in (option.short, option.long) if each}


def implementation(version: str) -> str | None:
    """The implementation a program's --version names on its first line, GNU or uutils; None for any other."""
    lines = version.strip().splitlines()
    match = VERSION_LINE.match(lines[0]) if lines else None
    return match[2] if match else None


def installed_implementation(version: str) -> str | None:
    """The implementation of a wrapper the system installs: the one its --version names, or on macOS, whose own
    wrappers are Apple's builds of FreeBSD's and name none, BSD."""
    return implementation(version) or ("BSD" if platform_support.MACOS else None)


def synopsis_word(word: str, valued: bool) -> list[Option]:
    """The options one option word of a synopsis names."""
    if word.startswith("--"):
        return [Option(None, word, "value" if valued else "none")]
    body = word[1:]
    if PLACEHOLDER.fullmatch(body):
        return []
    if PLACEHOLDER.fullmatch(body[1:]) or (valued and len(body) == 1):
        return [Option(word[:2], None, "value")]
    return [Option(f"-{letter}", None, "none") for letter in body]


def usage_options(text: str, program: str) -> tuple[Option, ...]:
    """The options a BSD build's usage synopsis lists, from its `usage: <program>` line, in either case, and the
    indented lines after it, each with whether it takes a value."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.casefold().startswith(f"usage: {program} ")), None)
    if start is None:
        return ()
    synopsis = [lines[start]]
    synopsis += itertools.takewhile(lambda line: line[:1].isspace(), lines[start + 1 :])
    found: list[Option] = []
    for match in USAGE_OPTION.finditer(" ".join(synopsis)):
        for word in match.group(1).split("|"):
            found += synopsis_word(word.strip(), match.group(2) is not None)
    return tuple(dict.fromkeys(found))


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


def getopt_letters(program: str) -> dict[str, bool] | None:
    """The letters of a getopt-only build macOS installs, from its catalogue entry's getopt string, each with whether it
    takes a value; None for a program with no such build."""
    entry = catalogue(BSD_CATALOGUES).get(program)
    if entry is None or "getopt" not in entry:
        return None
    return {match[1]: bool(match[2]) for match in GETOPT_LETTER.finditer(str(entry["getopt"]))}


def bsd_getopt(program: str, words: list[str]) -> tuple[int, list[tuple[str, str | None]]] | None:
    """A getopt-only build's reading of its words as FreeBSD's getopt(3) reads them: where its options end, with each
    letter it read and that letter's value; None where it refuses a word, printing its usage and running nothing. `--`
    ends the options; a word without a dash, or a lone `-` where `-` is no letter, is the first after them; any other
    dash word is a bundle of letters, `--unset` the letter `-` and more, a value-taking letter ending it with the rest
    of the word or the next word as its value. nice reads an obsolete first word, -<number> or --<number>, as -n with
    the rest attached, and refuses a niceness strtol does not read whole."""
    letters = getopt_letters(program)
    assert letters is not None
    words = list(words)
    if program == "nice" and words and words[0][:1] == "-" and words[0][1:2] in {"-", *"0123456789"}:
        words[0] = words[0] if words[0] == "--" else "-n" + words[0][1:]
    read: list[tuple[str, str | None]] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or (word == "-" and "-" not in letters):
            break
        body, taken = word[1:] or "-", 0
        for position, letter in enumerate(body):
            if letter not in letters:
                return None
            value = None
            if letters[letter]:
                value = body[position + 1 :]
                if not value:
                    if index + 1 >= len(words):
                        return None
                    value, taken = words[index + 1], 1
            read.append((letter, value))
            if value is not None:
                break
        index += 1 + taken
    if program == "nice" and any(not NICENESS.fullmatch(value or "") for _, value in read):
        return None
    return index, read


def find_bash() -> str:
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash lists the wrappers' options the fixture is held to")
    return bash


@functools.cache
def installed_help() -> dict[str, tuple[str, str, str]]:
    """The --help, the --version, and the refusal of an unknown option of each wrapper CHECKED or MACOS_CHECKED names
    that the system installs."""
    workspace = Path(WORKSPACE.name)
    programs = (*CHECKED, *MACOS_CHECKED)
    result = platform_support.run_tool(
        [find_bash(), "-c", DISCOVER, "discover", platform_support.normalize(workspace), *programs], timeout=120
    )
    if result.returncode != 0:
        raise AssertionError(f"Bash could not list the wrappers: {result.output.strip()}")
    return {
        name: (
            (workspace / f"help.{name}").read_text(encoding="utf-8", errors="replace"),
            (workspace / f"version.{name}").read_text(encoding="utf-8", errors="replace"),
            (workspace / f"usage.{name}").read_text(encoding="utf-8", errors="replace"),
        )
        for name in programs
        if (workspace / f"path.{name}").is_file()
    }


class CatalogueTests(unittest.TestCase):
    def test_each_wrapper_row_reads_every_option_its_gnu_and_rust_builds_list_as_they_take_it(self) -> None:
        # command_wrappers.py places a wrapper's program past every option either build takes, so its row holds the
        # union of the two catalogues, each option taking its value as both list it. findutils' getopt table takes
        # --max-lines's value as optional, though its --help, which the catalogue records, writes it as taking one.
        exceptions = {("xargs", "--max-lines"): "optional"}
        kinds = {"value": option_reader.VALUE, "none": option_reader.NO_VALUE, "optional": option_reader.OPTIONAL_VALUE}
        held = [
            name for name in COMMAND_WRAPPERS if name in catalogue(CATALOGUES) or name in catalogue(RUST_CATALOGUES)
        ]
        self.assertLessEqual({"env", "nice", "nohup", "stdbuf", "timeout", "chroot", "xargs"}, set(held))
        for name in held:
            with self.subTest(program=name):
                table = COMMAND_WRAPPERS[name]
                row = {f"-{letter}": kind for letter, kind in table.short.items()} | dict(table.long)
                listed: dict[str, str] = {}
                for path in (CATALOGUES, RUST_CATALOGUES):
                    if name in catalogue(path):
                        for option in listed_options(path, name):
                            for each in (option.short, option.long):
                                if each:
                                    kind = exceptions.get((name, each), option.value)
                                    self.assertEqual(kinds[kind], listed.setdefault(each, kinds[kind]), each)
                self.assertEqual(listed, row)

    def test_every_catalogue_holds_only_wrappers_each_with_its_provenance(self) -> None:
        for path in IMPLEMENTATIONS.values():
            for name, entry in catalogue(path).items():
                with self.subTest(catalogue=path.name, program=name):
                    self.assertIn(name, COMMAND_WRAPPERS)
                    self.assertTrue(str(entry["source"]).strip())
        self.assertEqual(["chroot", "env", "nice", "nohup", "stdbuf", "timeout"], sorted(catalogue(RUST_CATALOGUES)))
        self.assertEqual(
            ["chroot", "env", "nice", "nohup", "stdbuf", "time", "xargs"], sorted(catalogue(BSD_CATALOGUES))
        )

    def test_every_row_reads_dash_words_as_each_macos_build_that_takes_dash_as_a_letter(self) -> None:
        # A build whose getopt lists `-` reads every long option word as a bundle of its letters, so each row of a
        # program with such a build holds that build's letters; a row of any other program holds none.
        dashed = {
            name: {letter: "value" if takes else "none" for letter, takes in letters.items()}
            for name in catalogue(BSD_CATALOGUES)
            if (letters := getopt_letters(name)) is not None and "-" in letters
        }
        self.assertEqual(["env"], sorted(dashed))
        for name, table in COMMAND_WRAPPERS.items():
            with self.subTest(wrapper=name):
                build = dashed.get(name)
                self.assertEqual(build, None if table.dash_build is None else dict(table.dash_build))
                self.assertEqual(set(build or ()), set(table.dash_safe))

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

    def lists_every_option(self, name: str) -> None:
        helps = installed_help()
        if name not in helps:
            self.skipTest(f"{name} is not installed here")
        text, version, usage = helps[name]
        first = (version.strip().splitlines() or [""])[0]
        build = installed_implementation(version)
        if name in MACOS_CHECKED and build != "BSD":
            # A GNU or Rust build of one of these is held to its catalogue alone; see MACOS_CHECKED.
            return
        self.assertIsNotNone(build, f"{name}'s --version names neither GNU nor uutils: {first!r}")
        assert build is not None
        path = IMPLEMENTATIONS[build]
        self.assertIn(name, catalogue(path), f"{path.name} has no catalogue of {name}, which is {first!r} here")
        listed = names(listed_options(path, name))
        bsd = path is BSD_CATALOGUES
        options = usage_options(usage, name) if bsd else help_options(text)
        if bsd and catalogue(path)[name]["options"]:
            # A synopsis the reader finds nothing in would hold the catalogue to nothing.
            self.assertTrue(options, f"{name} printed no options this suite reads: {usage.strip()[:400]!r}")
        for option in options:
            for each in (option.short, option.long):
                if each is not None:
                    with self.subTest(option=each):
                        self.assertIn(each, listed, f"{first} lists an option {path.name}'s {name} does not")

    def test_the_installed_runners_list_no_option_the_fixture_leaves_out(self) -> None:
        for name in CHECKED:
            with self.subTest(program=name):
                self.lists_every_option(name)

    def test_the_wrappers_macos_builds_of_its_own_list_no_option_the_fixture_leaves_out(self) -> None:
        for name in MACOS_CHECKED:
            with self.subTest(program=name):
                self.lists_every_option(name)


# macOS's builds of the wrappers whose catalogue entry gives a getopt string, read as FreeBSD's getopt reads them.
class MacosBuildTests(unittest.TestCase):
    def test_a_long_option_macos_s_env_reads_as_a_bundle_never_hides_its_command(self) -> None:
        # Apple's env reads --unset as `-` and -u with nset attached, so tar and find are the programs it runs; the
        # shared reader leaves such a word's program unread rather than take another word for it.
        self.assertEqual((1, [("-", None), ("u", "nset")]), bsd_getopt("env", ["--unset", "tar", "xf", "a.tar"]))
        for words in (["--unset", "tar", "xf", "a.tar"], ["--unset", "find", ".", "-delete"], ["-0", "--unset", "tar"]):
            with self.subTest(words=words):
                [read] = unwrapped(["env", *words])
                self.assertTrue(read.startswith(UNREAD_PROGRAM), read)
        # A word Apple's env refuses runs nothing there, and one it reads alike places the program after it.
        self.assertIsNone(bsd_getopt("env", ["--null", "ls"]))
        for words in (["--unset=X", "ls", "-la"], ["--null", "--debug", "ls", "-la"], ["-u", "X", "ls", "-la"]):
            with self.subTest(words=words):
                self.assertEqual(["ls", "-la"], unwrapped(["env", *words]))

    def test_macos_s_getopt_only_builds_are_read_as_freebsd_s_getopt_reads_them(self) -> None:
        for program, words, expected in (
            ("env", ["-i", "-", "-u", "X", "ls"], (4, [("i", None), ("-", None), ("u", "X")])),
            ("env", ["-iuX", "--", "ls"], (2, [("i", None), ("u", "X")])),
            ("env", ["-S"], None),
            ("time", ["--portability", "ls"], None),
            ("time", ["-p", "ls"], (1, [("p", None)])),
            ("chroot", ["/srv", "ls"], (0, [])),
            ("nohup", ["--help", "ls"], None),
            ("nice", ["--5", "ls"], (1, [("n", "-5")])),
            ("nice", ["-5", "ls"], (1, [("n", "5")])),
            ("nice", ["--adjustment", "5", "ls"], None),
            ("nice", ["-n", "x", "ls"], None),
            ("stdbuf", ["-oL", "-e", "0", "ls"], (3, [("o", "L"), ("e", "0")])),
            ("stdbuf", ["--output=L", "ls"], None),
        ):
            with self.subTest(program=program, words=words):
                self.assertEqual(expected, bsd_getopt(program, words))


class ReaderTests(unittest.TestCase):
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

    def test_reads_apple_chroot_s_time_s_and_stdbuf_s_synopses_each_option_in_its_row(self) -> None:
        chroot = (
            "chroot: illegal option -- @\nusage: chroot [-g group] [-G group,group,...] [-u user] newroot [command]\n"
        )
        time = "time: illegal option -- @\nusage: time [-al] [-h | -p] [-o file] utility [argument ...]\n"
        stdbuf = (
            "stdbuf: illegal option -- @\n"
            "usage: stdbuf [-e 0|L|B|<sz>] [-i 0|L|B|<sz>] [-o 0|L|B|<sz>] <cmd> [args ...]\n"
        )
        for name, text, expected in (
            ("chroot", chroot, {Option(each, None, "value") for each in ("-g", "-G", "-u")}),
            ("stdbuf", stdbuf, {Option(each, None, "value") for each in ("-e", "-i", "-o")}),
            (
                "time",
                time,
                {*(Option(each, None, "none") for each in ("-a", "-l", "-h", "-p")), Option("-o", None, "value")},
            ),
        ):
            with self.subTest(program=name):
                self.assertEqual(expected, set(usage_options(text, name)))
                self.assertLessEqual(expected, set(listed_options(BSD_CATALOGUES, name)))

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
            (Option("-i", None, "none"),), usage_options("usage: env [-i] utility\nother: [-q] x\n", "env")
        )

    def test_reads_short_long_and_value_kinds_from_a_help(self) -> None:
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

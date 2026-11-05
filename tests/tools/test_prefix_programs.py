"""Holds tools/hub_guard.py's option allow-lists to each runner's and program's real option catalogue: while a hub is
guarded, every option word of a program RUNNER_OPTIONS or PROGRAM_OPTIONS names is refused unless it is one this
suite names as safe, in every spelling getopt reads, and a runner whose command is or may be git is refused whatever
its options.

The rows come from tests/fixtures/hub-guard/option-catalogues.json, each program's options as its --help or its getopt
table lists them, never from the guard's tables: each option alone, with its value attached and after it,
abbreviated where the program reads an abbreviation, and bundled with each other short option. Which options are
safe is SAFE below, a literal of this suite's own, and the guard's tables are held to it. Where the system installs a
runner, Git Bash's on Windows and the system's elsewhere, its own --help is read too, and every option it lists must be
in the fixture, so a release that adds an option fails here until the fixture and SAFE decide it.
"""

from __future__ import annotations

import functools
import json
import re
import shlex
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from deployer import platform_support
from tools import hub_guard

SKIP_ALLOWED = {
    "CatalogueTests.lists_every_option": "a program the system does not install has no --help to read",
}

CATALOGUES = Path(__file__).resolve().parents[1] / "fixtures" / "hub-guard" / "option-catalogues.json"
# The options this suite holds safe: they neither run a command nor build one from text, change where one runs,
# write a file the command does not name, nor change what the guard reads.
SAFE: dict[str, set[str]] = {
    "env": {"-i", "--ignore-environment", "-0", "--null", "-u", "--unset", "-v", "--debug"},
    "xargs": {
        *("-0", "--null", "-a", "--arg-file", "-d", "--delimiter", "-E", "-L", "-n", "--max-args", "-P"),
        *("--max-procs", "-r", "--no-run-if-empty", "-s", "--max-chars", "-t", "--verbose", "-x", "--exit"),
    },
    "timeout": {"-k", "--kill-after", "-s", "--signal", "-v", "--verbose", "--preserve-status", "--foreground"},
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
        *("--field-separator", "--parallel", "-u", "--unique", "-z", "--zero-terminated"),
    },
    "tar": {
        *("-A", "--catenate", "--concatenate", "-c", "--create", "--delete", "-d", "--diff", "--compare", "-r"),
        *("--append", "-t", "--list", "-u", "--update", "--ignore-failed-read", "--force-local"),
        *("--no-seek", "-n", "--seek", "-S", "--sparse", "--exclude", "--exclude-backups"),
        *("--exclude-caches", "--exclude-caches-all", "--exclude-caches-under", "--exclude-tag"),
        *("--exclude-tag-all", "--exclude-tag-under", "--exclude-vcs", "--exclude-vcs-ignores", "--no-null"),
        *("--no-recursion", "--no-unquote", "--no-verbatim-files-from", "--null", "--recursion", "-T"),
        *("--files-from", "--unquote", "--verbatim-files-from", "-X", "--exclude-from", "--anchored"),
        *("--ignore-case", "--no-anchored", "--no-ignore-case", "--no-wildcards", "--no-wildcards-match-slash"),
        *("--wildcards", "--wildcards-match-slash", "--keep-newer-files", "-k", "--keep-old-files"),
        *("--no-overwrite-dir", "--skip-old-files"),
        *("-W", "--verify", "-O", "--to-stdout", "--clamp-mtime", "--delay-directory-restore", "--group"),
        *("--mode", "--mtime", "-m", "--touch", "--no-delay-directory-restore", "--no-same-owner"),
        *("--no-same-permissions", "--numeric-owner", "--owner", "-p", "--preserve-permissions"),
        *("--same-permissions", "--same-owner", "--sort", "-s", "--acls", "--no-acls", "--no-selinux"),
        *("--no-xattrs", "--selinux", "--xattrs", "-f", "--file", "-b", "--blocking-factor", "-B"),
        *("--read-full-records", "-i", "--ignore-zeros", "--record-size", "-H", "--format", "--old-archive"),
        *("--portability", "--posix", "-V", "--label", "-a", "--auto-compress", "-j", "--bzip2", "-J", "--xz"),
        *("--lzip", "--lzma", "--lzop", "--no-auto-compress", "--zstd", "-z", "--gzip", "--gunzip", "--ungzip"),
        *("-Z", "--compress", "--uncompress", "--hard-dereference", "-h", "--dereference", "-K", "--newer-mtime"),
        *("-N", "--newer", "--after-date", "--one-file-system", "-P", "--absolute-names", "--strip-components"),
        *("--transform", "--xform", "--full-time", "-l", "--check-links", "--quoting-style", "-R"),
        *("--block-number", "--show-omitted-dirs", "--show-transformed-names", "--show-stored-names", "--utc"),
        *("-v", "--verbose", "--warning", "-o", "--starting-file"),
    },
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
        (["-tf", "./a:b.tar"], True),
        # A value given to an option that takes none.
        (["--verbose=1", "-tf", "a.tar"], False),
        (["--verbose", "-tf", "a.tar"], True),
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
DISCOVER = r"""cd "$1" || exit 1
shift
for name in "$@"; do
  path=$(type -P "$name") || continue
  printf '%s\n' "$path" > "path.$name"
  "$path" --help < /dev/null > "help.$name" 2>&1
done"""
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
def catalogues() -> dict[str, dict[str, object]]:
    loaded: dict[str, dict[str, object]] = json.loads(CATALOGUES.read_text(encoding="utf-8"))
    return loaded


def options_of(program: str) -> list[Option]:
    entries = catalogues()[program]["options"]
    assert isinstance(entries, list)
    return [Option(entry["short"], entry["long"], entry["value"]) for entry in entries]


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
def installed_help() -> dict[str, str]:
    """The --help of each runner CHECKED names that the system installs."""
    workspace = Path(WORKSPACE.name)
    result = platform_support.run_tool(
        [find_bash(), "-c", DISCOVER, "discover", platform_support.normalize(workspace), *CHECKED], timeout=120
    )
    if result.returncode != 0:
        raise AssertionError(f"Bash could not list the runners: {result.output.strip()}")
    return {
        name: (workspace / f"help.{name}").read_text(encoding="utf-8", errors="replace")
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


class CatalogueTests(unittest.TestCase):
    def test_every_program_the_guard_reads_options_of_has_a_catalogue_a_test_and_its_safe_options(self) -> None:
        tables = {**hub_guard.RUNNER_OPTIONS, **hub_guard.PROGRAM_OPTIONS}
        self.assertEqual(sorted(tables), sorted(catalogues()))
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
                listed = {each for option in options_of(name) for each in (option.short, option.long) if each}
                self.assertLessEqual(SAFE[name], listed, "a safe option the program does not list")

    def test_every_runner_but_the_shells_and_the_ones_always_refused_reads_its_options(self) -> None:
        readers = set(hub_guard.RUNNERS) - hub_guard.RUNNERS_ALWAYS_REFUSED - {"bash", "pwsh"}
        self.assertEqual(sorted(readers), sorted(hub_guard.RUNNER_OPTIONS))
        self.assertEqual(set(), set(hub_guard.PROGRAM_OPTIONS) & set(hub_guard.RUNNERS))

    def test_find_s_actions_that_run_a_program_are_never_safe(self) -> None:
        self.assertEqual({"-exec", "-execdir", "-ok", "-okdir"}, hub_guard.FIND_COMMANDS)
        self.assertEqual(set(), hub_guard.FIND_COMMANDS & SAFE["find"])

    def lists_every_option(self, name: str) -> None:
        helps = installed_help()
        if name not in helps:
            self.skipTest(f"{name} is not installed here")
        listed = {each for option in options_of(name) for each in (option.short, option.long) if each}
        for option in help_options(helps[name]):
            for each in (option.short, option.long):
                if each is not None:
                    with self.subTest(option=each):
                        self.assertIn(each, listed, f"{name} lists an option the fixture does not")

    def test_the_installed_runners_list_no_option_the_fixture_leaves_out(self) -> None:
        for name in CHECKED:
            with self.subTest(program=name):
                self.lists_every_option(name)


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

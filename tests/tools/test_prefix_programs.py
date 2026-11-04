"""Holds tools/hub_guard.py's runner rule to every option form the runners the system installs list: while a hub is
guarded, a runner whose command is or may be git is refused, whatever option spelling stands before that command.

Each runner the system installs, Git Bash's on Windows and the system's elsewhere, gives its own --help, and every form
of every option it lists, attached, after its option, abbreviated, bundled, and doubled, alongside the hub guard
suite's examples, stands before git in a command the guard reads as data. The guard must refuse each, and must leave
the same forms before a program that is not git as the rule decides: understood, but for an option that builds the
command from text, which is refused whatever it runs.
"""

from __future__ import annotations

import functools
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
    "RunnerFormTests.refuses_git": "a program the system does not install has no --help to take its forms from",
}

# The runners whose words stand before the command they run, each checked on every form its own --help lists.
CHECKED = ("env", "nice", "nohup", "stdbuf", "timeout", "xargs")
# The operands each program takes before its command, as its manual gives them.
OPERANDS = {"timeout": ["5"]}
# A value each option that takes one accepts, by program and long name; any other takes VALUE.
VALUES = {
    "env": {"chdir": "d1", "unset": "X"},
    "nice": {"adjustment": "5"},
    "timeout": {"signal": "TERM", "kill-after": "5"},
    "stdbuf": {"input": "0", "output": "L", "error": "L"},
    "xargs": {"max-args": "1", "max-procs": "1", "delimiter": "x"},
}
SECOND_VALUES: dict[tuple[str, str], str] = {}
VALUE = "5"
OPTIONAL_VALUE = "INT"
# The hub guard suite's examples of each runner's options.
FIXTURE_FORMS = {
    "env": [["-i"], ["-u", "X"], ["-C", "d1"], ["-"], ["X=1"], ["-S", "x"]],
    "nice": [["-n5"], ["-n", "5"], ["--adjustment=5"], ["-5"], ["-n", "5", "-n", "3"]],
    "stdbuf": [["-oL"], ["-oL", "-eL"], ["-o", "L"]],
    "timeout": [["-k5"], ["-s", "TERM"], ["-vk5"], ["--signal=TERM"], ["--sig=TERM"], ["--"]],
    "nohup": [["--"]],
    "xargs": [["-0"], ["-n", "1"], ["-I{}"], ["-i"], ["--replace=x"], ["-r"]],
}
HELP_OPTION = re.compile(r"^\s+(?:(-[A-Za-z0-9]),?\s*)?(?:(--[a-z][a-z0-9-]*)(\[=|=)?)?")
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
    value: str  # "none", "required", or "optional", which only a long option's `=` gives


@dataclass(frozen=True)
class Program:
    name: str
    options: tuple[Option, ...]


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
        value = {"[=": "optional", "=": "required"}.get(equals or "", "none")
        if value == "none" and short is not None and long is None:
            words = line.split()
            value = "required" if len(words) > 1 and words[1].isupper() else "none"
        found.append(Option(short, long, value))
    return tuple(dict.fromkeys(found))


def value_for(program: str, option: Option, second: bool = False) -> str:
    name = (option.long or "").removeprefix("--")
    if second and (program, name) in SECOND_VALUES:
        return SECOND_VALUES[program, name]
    return VALUES.get(program, {}).get(name, VALUE)


def option_forms(program: str, option: Option) -> list[list[str]]:
    """The ways one option can be written: alone or with its value attached or after it, a long option with `=` and
    abbreviated, and a value-taking short option given twice."""
    value = value_for(program, option)
    forms: list[list[str]] = []
    if option.short is not None:
        forms += [[option.short]] if option.value != "required" else [[option.short, value], [option.short + value]]
        if option.value == "required":
            forms.append([option.short, value, option.short, value_for(program, option, second=True)])
    if option.long is not None:
        spellings = list(dict.fromkeys([option.long, option.long[:3], option.long[:-1]]))
        for spelling in spellings:
            if len(spelling) < 3:
                continue
            if option.value == "none":
                forms.append([spelling])
            elif option.value == "optional":
                forms += [[spelling], [f"{spelling}={OPTIONAL_VALUE}"]]
            else:
                forms += [[spelling, value], [f"{spelling}={value}"]]
    return forms


def bundles(program: str, options: tuple[Option, ...]) -> list[list[str]]:
    """Each short option that takes no value bundled with each other short option, the second's value attached and
    after the bundle."""
    shorts = [option for option in options if option.short is not None and option.short not in {"-h"}]
    forms: list[list[str]] = []
    for first in shorts:
        if first.value != "none" or first.short is None:
            continue
        for second in shorts:
            if second is first or second.short is None:
                continue
            bundle = first.short + second.short[1:]
            if second.value == "required":
                value = value_for(program, second)
                forms += [[bundle + value], [bundle, value]]
            else:
                forms.append([bundle])
    return forms


def program_forms(program: Program) -> list[list[str]]:
    """Every form of a runner's options, as its words before the command it runs."""
    operands = OPERANDS.get(program.name, [])
    option_lists: list[list[str]] = [[]]
    for option in program.options:
        if option.long not in {"--help", "--version"}:
            option_lists += option_forms(program.name, option)
    option_lists += bundles(program.name, program.options)
    option_lists += FIXTURE_FORMS.get(program.name, [])
    forms = [[*options, *operands] for options in option_lists]
    forms += [["--", *operands], *([[*options, "--", *operands] for options in option_lists[1:4]])]
    # Options after the operands, which a program that permutes its arguments would still read.
    if operands:
        forms += [[*operands, *options] for options in option_lists[1:] if len(options) == 1]
    return list(map(list, dict.fromkeys(tuple(form) for form in forms)))


def find_bash() -> str:
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash lists the runners' options the guard is held to")
    return bash


@functools.cache
def discover() -> dict[str, Program]:
    """Each runner the system installs, with the options its --help lists."""
    workspace = Path(WORKSPACE.name)
    result = platform_support.run_tool(
        [find_bash(), "-c", DISCOVER, "discover", platform_support.normalize(workspace), *CHECKED], timeout=120
    )
    if result.returncode != 0:
        raise AssertionError(f"Bash could not list the runners: {result.output.strip()}")
    programs: dict[str, Program] = {}
    for name in CHECKED:
        if (workspace / f"path.{name}").is_file():
            text = (workspace / f"help.{name}").read_text(encoding="utf-8", errors="replace")
            programs[name] = Program(name, help_options(text))
    return programs


def refused(command: str) -> bool:
    reading = hub_guard.Reading({}, command)
    hub_guard.read_bash(command, Path.cwd(), reading)
    return bool(reading.unread)


class RunnerFormTests(unittest.TestCase):
    def refuses_git(self, name: str) -> None:
        programs = discover()
        if name not in programs:
            self.skipTest(f"{name} is not installed here")
        forms = program_forms(programs[name])
        self.assertTrue(forms, f"{name} gave no forms")
        building = hub_guard.RUNNER_BUILDING_OPTIONS.get(name, frozenset())
        for words in forms:
            prefix = shlex.join(words)
            with self.subTest(form=prefix):
                self.assertTrue(refused(f"{name} {prefix} git status"), "a runner before git is refused")
                builds = any(
                    word.partition("=")[0] in building
                    or any(len(each) == 2 and word.startswith(each) for each in building)
                    for word in words
                )
                self.assertEqual(builds, refused(f"{name} {prefix} ls"), "only an option that builds the command")

    def test_env(self) -> None:
        self.refuses_git("env")

    def test_nice(self) -> None:
        self.refuses_git("nice")

    def test_nohup(self) -> None:
        self.refuses_git("nohup")

    def test_stdbuf(self) -> None:
        self.refuses_git("stdbuf")

    def test_timeout(self) -> None:
        self.refuses_git("timeout")

    def test_xargs(self) -> None:
        self.refuses_git("xargs")

    def test_every_checked_runner_has_a_test_and_is_a_runner(self) -> None:
        tested = {name.removeprefix("test_") for name in dir(self) if name.startswith("test_")}
        self.assertLessEqual(set(CHECKED), tested)
        self.assertLessEqual(set(CHECKED), set(hub_guard.RUNNERS))


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
                Option("-C", "--chdir", "required"),
                Option(None, "--block-signal", "optional"),
                Option(None, "--help", "none"),
            ),
            help_options(text),
        )

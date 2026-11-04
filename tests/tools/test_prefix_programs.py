"""Holds tools/hub_guard.py's reading of the runners whose options it reads (RUNNER_OPTIONS) to the real programs,
and pins every other runner as one the guard refuses.

Each program the system installs, Git Bash's on Windows and the system's elsewhere, runs every option form below with
its command replaced by a probe that records its arguments, its directory, and one variable. The guard reads the same
words, and for each form must either read the command the program started or decline to read it, which refuses it
while a hub is guarded; it must never read another. A form the program refuses, or one after which it starts nothing,
holds the guard to nothing: judging a command that never runs refuses only more. The forms come from each program's
own --help, so an option the guard's tables do not list is tried too, alongside the hub guard suite's examples.
"""

from __future__ import annotations

import functools
import re
import shlex
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from deployer import platform_support
from tools import hub_guard

SKIP_ALLOWED = {
    "PrefixProgramAgreementTests.agree": ("a program the system does not install has no behavior to hold the guard to"),
}

# The variable whose value the probe records: set to OUTER for every form, so env's -i, -u, and NAME=value show.
VARIABLE = "HUB_GUARD_PROBE_VARIABLE"
OUTER = "outer"
# The word that stands for the probe in what the guard reads; the probe records its own path as PROBE.
PROBE_WORD = "/probe-root/probe"
PROBE = "PROBE"
# Where the guard starts each form, under a root that stands for the workspace.
GUARD_ROOT = Path("/probe-root")
START = "start"
DIRECTORIES = ("d1", "d2", "d1/d2")
# The file in each directory that holds its path below the workspace, which the probe reports as its directory.
MARKER = ".where"
# The operands each program takes before its command, as its manual gives them.
OPERANDS = {"timeout": ["5"]}
# A value each option that takes one accepts, by program and long name; any other takes VALUE.
VALUES = {
    "nice": {"adjustment": "5"},
    "timeout": {"signal": "TERM", "kill-after": "5"},
    "stdbuf": {"input": "0", "output": "L", "error": "L"},
}
SECOND_VALUES: dict[tuple[str, str], str] = {}
VALUE = "5"
OPTIONAL_VALUE = "INT"
# The hub guard suite's examples of each program's options.
FIXTURE_FORMS = {
    "nice": [["-n5"], ["-n", "5"], ["--adjustment=5"], ["-5"], ["-n", "5", "-n", "3"]],
    "stdbuf": [["-oL"], ["-oL", "-eL"], ["-o", "L"]],
    "timeout": [["-k5"], ["-s", "TERM"], ["-vk5"], ["--signal=TERM"], ["--sig=TERM"], ["--"]],
    "nohup": [["--"]],
}
HELP_OPTION = re.compile(r"^\s+(?:(-[A-Za-z0-9]),?\s*)?(?:(--[a-z][a-z0-9-]*)(\[=|=)?)?")
DISCOVER = r"""cd "$1" || exit 1
shift
for name in "$@"; do
  path=$(type -P "$name") || continue
  printf '%s\n' "$path" > "path.$name"
  "$path" --version < /dev/null > "version.$name" 2>&1
  "$path" --help < /dev/null > "help.$name" 2>&1
done"""
# The probe a form starts: it records, under out/ and named by its last argument, its directory below the workspace,
# whether VARIABLE is set and to what, and its arguments, its own path given as PROBE, each ending in a NUL.
PROBE_SCRIPT = f"""#!/bin/sh
root=${{0%/*}}
for last; do :; done
{{
  # Each directory names itself in a file, since its path may be spelled two ways, as with a Windows short name.
  IFS= read -r here < {MARKER} || here=unknown
  printf '%s\\0' "$here"
  if [ -n "${{{VARIABLE}+x}}" ]; then printf 'set:%s\\0' "${VARIABLE}"; else printf 'unset\\0'; fi
  for argument; do
    if [ "$argument" = "$0" ]; then argument={PROBE}; fi
    printf '%s\\0' "$argument"
  done
}} > "$root/out/$last"
"""
# How many forms run at once.
BATCH = 16
WORKSPACE = tempfile.TemporaryDirectory(prefix="prefix-programs.")


@dataclass(frozen=True)
class Option:
    short: str | None
    long: str | None
    value: str  # "none", "required", or "optional", which only a long option's `=` gives


@dataclass(frozen=True)
class Started:
    """A command a program starts: its arguments after the probe's own path, its directory below the workspace, and
    VARIABLE's state; a directory or state None is one the guard leaves unsettled, which refuses where it matters."""

    arguments: tuple[str, ...]
    directory: str | None
    variable: str | None


@dataclass(frozen=True)
class Program:
    name: str
    path: str
    version: str
    options: tuple[Option, ...]


@dataclass(frozen=True)
class Runs:
    programs: dict[str, Program]
    # Each form a program ran, by program: its words before the probe, the number the probe records it by, and what it
    # started, or None for nothing.
    forms: dict[str, list[tuple[list[str], int, Started | None]]]


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
    """Every form a program runs, as its words before the probe."""
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
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash runs the programs the guard is held to")
    return bash


def run_bash(script: str, *arguments: str) -> None:
    result = platform_support.run_tool([find_bash(), "-c", script, "agreement", *arguments], timeout=300)
    if result.returncode != 0:
        raise AssertionError(f"Bash could not run the programs: {result.output.strip()}")


def discover(workspace: Path) -> dict[str, Program]:
    names = sorted(set(hub_guard.RUNNER_OPTIONS) - {"xargs"})
    run_bash(DISCOVER, platform_support.normalize(workspace), *names)
    programs: dict[str, Program] = {}
    for name in names:
        path = workspace / f"path.{name}"
        if not path.is_file():
            continue
        version = (workspace / f"version.{name}").read_text(encoding="utf-8", errors="replace").strip()
        help_text = (workspace / f"help.{name}").read_text(encoding="utf-8", errors="replace")
        programs[name] = Program(
            name,
            path.read_text(encoding="utf-8").strip(),
            (version.splitlines() or ["(no version)"])[0],
            help_options(help_text),
        )
    return programs


def form_line(program: Program, words: list[str], index: int) -> str:
    quoted = " ".join(shlex.quote(word) for word in words)
    return (
        f'( cd "$ROOT/{START}" && {VARIABLE}={OUTER} && export {VARIABLE} && '
        f'exec {shlex.quote(program.path)} {quoted} "$ROOT/probe" "$ROOT/probe" -v f{index} ) '
        "< /dev/null > /dev/null 2>&1 &"
    )


def record(path: Path) -> Started | None:
    if not path.is_file():
        return None
    fields = path.read_bytes().decode("utf-8", "replace").split("\0")[:-1]
    directory, variable, arguments = fields[0], fields[1], fields[2:]
    return Started(tuple(arguments), directory, variable)


@functools.cache
def real_runs() -> Runs:
    workspace = Path(WORKSPACE.name)
    programs = discover(workspace)
    for directory in ("", *DIRECTORIES):
        place = workspace / START / directory
        place.mkdir(parents=True, exist_ok=True)
        (place / MARKER).write_bytes(PurePosixPath(START, directory).as_posix().encode("utf-8") + b"\n")
    (workspace / "out").mkdir()
    probe = workspace / "probe"
    probe.write_bytes(PROBE_SCRIPT.encode("utf-8"))
    probe.chmod(0o755)
    lines = ['ROOT=$(cd "$1" && pwd) || exit 1']
    planned: dict[str, list[tuple[list[str], int]]] = {}
    index = 0
    for program in programs.values():
        for words in program_forms(program):
            planned.setdefault(program.name, []).append((words, index))
            lines.append(form_line(program, words, index))
            index += 1
            if index % BATCH == 0:
                lines.append("wait")
    lines.append("wait")
    (workspace / "forms.sh").write_bytes("\n".join(lines).encode("utf-8"))
    run_bash('. "$1/forms.sh"', platform_support.normalize(workspace))
    forms = {
        name: [(words, number, record(workspace / "out" / f"f{number}")) for words, number in entries]
        for name, entries in planned.items()
    }
    return Runs(programs, forms)


def guard_reading(program: str, words: list[str], index: int) -> Started | str | None:
    """What the guard reads a form to start: a Started, None for nothing, or "declined" for a form it cannot read.
    Each runner starts its command in its own directory with its own environment."""
    arguments: list[hub_guard.Word] = [*words, PROBE_WORD, PROBE_WORD, "-v", f"f{index}"]
    start, reason = hub_guard.runner_start(program, arguments)
    if reason is not None:
        return "declined"
    if start is None:
        return None
    name, rest = arguments[start], arguments[start + 1 :]
    if name != PROBE_WORD:
        return Started(("(not the probe)", str(name)), None, None)
    shown = tuple(PROBE if word == PROBE_WORD else str(word) for word in rest)
    return Started(shown, START, f"set:{OUTER}")


def agrees(real: Started | None, guard: Started | str | None) -> bool:
    if real is None or guard == "declined":
        return True
    if not isinstance(guard, Started):
        return False
    return (
        guard.arguments == real.arguments
        and guard.directory in {None, real.directory}
        and guard.variable in {None, real.variable}
    )


class PrefixProgramAgreementTests(unittest.TestCase):
    def agree(self, name: str) -> None:
        runs = real_runs()
        if name not in runs.programs:
            self.skipTest(f"{name} is not installed here")
        program = runs.programs[name]
        print(f"{name}: {program.version}")
        forms = runs.forms.get(name, [])
        self.assertTrue(forms, f"no form of {name} ran")
        self.assertTrue(any(real is not None for _, _, real in forms), f"{name} started the probe from no form")
        disagreements = []
        for words, number, real in forms:
            guard = guard_reading(name, words, number)
            if not agrees(real, guard):
                disagreements.append(f"{name} {shlex.join(words)}: the program started {real}, the guard read {guard}")
        self.assertEqual([], disagreements, f"{program.version}")

    def test_timeout(self) -> None:
        self.agree("timeout")

    def test_nice(self) -> None:
        self.agree("nice")

    def test_nohup(self) -> None:
        self.agree("nohup")

    def test_stdbuf(self) -> None:
        self.agree("stdbuf")

    def test_every_runner_whose_options_are_read_is_held_to_its_program(self) -> None:
        tested = {name.removeprefix("test_") for name in dir(self) if name.startswith("test_")}
        self.assertLessEqual(set(hub_guard.RUNNER_OPTIONS) - {"xargs"}, tested)

    def test_every_runner_with_no_reader_is_refused_and_no_git_call_is_read_from_it(self) -> None:
        read = {*hub_guard.RUNNER_OPTIONS, "env", "bash", "sh", "pwsh"}
        for name in sorted(set(hub_guard.RUNNERS) - read):
            with self.subTest(program=name):
                reading = hub_guard.Reading({}, text="")
                hub_guard.read_bash(f"x=1 {name} git status", GUARD_ROOT, reading)
                self.assertTrue(reading.unread)
                self.assertEqual([], reading.calls, "no git call is read from a command it does not read")


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

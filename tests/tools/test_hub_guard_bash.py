"""The hub guard's Bash catalogues, each held to the list Bash gives of it: every builtin (`compgen -b`), reserved word
(`compgen -k`), `set -o` option, `shopt` option, and variable (`compgen -v`) Bash lists is understood or in NOT_READ,
and the rows below are generated from those tables, so an entry a later Bash adds fails here until a row decides it."""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement
import option_reader
from guard_limits import process_limits_held

from deployer import platform_support
from tools import bash_syntax, hub_guard

START = Path(__file__).resolve().parent


def reading(command: str, environment: dict[str, str] | None = None) -> hub_guard.Reading:
    """The guard's reading of a Bash command, as data: nothing is run."""
    result = hub_guard.Reading(environment or {}, command)
    with process_limits_held():
        hub_guard.read_bash(command, START, result)
    return result


def listed(script: str) -> list[str]:
    """The first word of each line a bash started with no environment and no startup files prints for script."""
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash lists the catalogues the guard is held to")
    command = [bash, "--norc", "--noprofile", "-c", f'env -i "$BASH" --norc --noprofile -c "{script}"']
    output = platform_support.run_tool(command).output
    names = [line.split()[0] for line in output.splitlines() if line.split()]
    if not names:
        raise AssertionError(f"Bash listed nothing for {script}")
    return names


def not_read(catalogue: str) -> set[str]:
    prefix = f"{catalogue} "
    return {entry.removeprefix(prefix) for entry in hub_guard.NOT_READ if entry.startswith(prefix)}


# A command that runs each understood builtin, where its name alone is not one.
BUILTIN_SAMPLES = {
    "command": "command -v git",
    "printf": "printf '%s' x",
    "test": "test -n x",
    "[": "[ -n x ]",
    "pushd": "pushd .",
    "read": "read -r line",
    "set": "set -e",
    "shopt": "shopt -s nullglob",
    "kill": "kill -0 1",
}
# A command holding each reserved word.
KEYWORD_SAMPLES = {
    **dict.fromkeys(("if", "then", "else", "elif", "fi"), "if true; then :; elif false; then :; else :; fi"),
    **dict.fromkeys(("case", "esac", "in"), "case x in x) : ;; esac"),
    **dict.fromkeys(("for", "do", "done"), "for x in a b; do :; done"),
    "select": "select x in a; do break; done",
    "while": "while false; do :; done",
    "until": "until true; do :; done",
    **dict.fromkeys(("{", "}"), "{ :; }"),
    "!": "! false",
    "time": "time true",
    **dict.fromkeys(("[[", "]]"), "[[ -n x ]]"),
    "function": "function f { :; }",
    "coproc": "coproc true",
}
# A word of each form EXPANSIONS_UNDERSTOOD and NOT_READ's expansions name.
EXPANSION_SAMPLES = {
    "$NAME": "$x",
    "${NAME}": "${x}",
    "$@": '"$@"',
    "$*": "$*",
    "$#": "$#",
    "$?": "$?",
    "$-": "$-",
    "$$": "$$",
    "$!": "$!",
    "$0": "$0",
    "$1": "$1",
    "$(": "$(date)",
    "`": "`date`",
    "<(": "<(date)",
    ">(": ">(cat)",
    "~": "~root",
    "$'": "$'a\\tb'",
    "glob": "*.md",
    "brace": "{a,b}",
    "${NAME:-word}": "${x:-y}",
    "${NAME-word}": "${x-y}",
    "${NAME:+word}": "${x:+y}",
    "${NAME+word}": "${x+y}",
    "${NAME:?word}": "${x:?y}",
    "${NAME?word}": "${x?y}",
    "${#NAME}": "${#x}",
    "${NAME#word}": "${x#y}",
    "${NAME##word}": "${x##y}",
    "${NAME%word}": "${x%y}",
    "${NAME%%word}": "${x%%y}",
    "${NAME/word}": "${x/a/b}",
    "${NAME//word}": "${x//a/b}",
    "${NAME/#word}": "${x/#a/b}",
    "${NAME/%word}": "${x/%a/b}",
    "${NAME^}": "${x^}",
    "${NAME^^}": "${x^^}",
    "${NAME,}": "${x,}",
    "${NAME,,}": "${x,,}",
    "${NAME:=word}": "${x:=y}",
    "${NAME=word}": "${x=y}",
    "$((": "$((1 + 2))",
    "$[": "$[1 + 2]",
    "NAME=( )": "x=(a b)",
    "${NAME[subscript]}": "${x[0]}",
    "${NAME:offset}": "${x:1}",
    "${!NAME}": "${!x}",
    "${NAME@op}": "${x@P}",
    "${ command; }": "${ date; }",
    "${| command; }": "${| REPLY=x; }",
    "nested ${": "${x:-${y}}",
    "${unread}": "${&x}",
    "assigning": "${PATH:=x}",
}


class BuiltinTests(unittest.TestCase):
    def test_every_builtin_bash_lists_is_understood_or_not_read_and_never_both(self) -> None:
        refused = not_read("builtin")
        self.assertEqual(set(), set(hub_guard.BUILTINS_UNDERSTOOD) & refused)
        self.assertEqual(set(), set(listed("compgen -b")) - set(hub_guard.BUILTINS_UNDERSTOOD) - refused)

    def test_each_builtin_is_read_as_its_table_says(self) -> None:
        for name in sorted(hub_guard.BUILTINS_UNDERSTOOD):
            with self.subTest(understood=name):
                self.assertEqual([], reading(BUILTIN_SAMPLES.get(name, name)).unread)
        for name in sorted(not_read("builtin")):
            with self.subTest(not_read=name):
                self.assertIn(hub_guard.NOT_READ[f"builtin {name}"], " ".join(reading(f"{name} x").unread))

    def test_an_understood_builtin_with_a_form_it_does_not_read_is_refused(self) -> None:
        for command in (
            "printf -v x %s y",
            "printf $f x",
            "command git status",
            "command",
            "wait -p x",
            "read -r PATH",
            "read -r $name",
            "read -a PATH",
            "read -X line",
            "test -v 'a[0]'",
            '[ $x "$y" ]',
            "cd -P /",
            "pushd -n /",
            "jobs -x git status",
            "jobs -lx git status",
            "jobs $x",
        ):
            with self.subTest(command=command):
                self.assertTrue(reading(command).unread)


class KeywordTests(unittest.TestCase):
    def test_every_reserved_word_bash_lists_is_understood_or_not_read_and_never_both(self) -> None:
        refused = not_read("keyword")
        self.assertEqual(set(), set(hub_guard.KEYWORDS_UNDERSTOOD) & refused)
        self.assertEqual(set(), set(listed("compgen -k")) - set(hub_guard.KEYWORDS_UNDERSTOOD) - refused)
        self.assertEqual(set(KEYWORD_SAMPLES), set(hub_guard.KEYWORDS_UNDERSTOOD) | refused)

    def test_each_reserved_word_is_read_as_its_table_says(self) -> None:
        bash_agreement.assert_reader_agrees(sorted(set(KEYWORD_SAMPLES.values()) - {"coproc true"}))
        for word, sample in KEYWORD_SAMPLES.items():
            with self.subTest(word=word):
                self.assertEqual(word in hub_guard.KEYWORDS_UNDERSTOOD, not reading(sample).unread)

    def test_the_command_the_time_keyword_times_is_read_after_each_option_bash_reads(self) -> None:
        # Bash reads -p right after time and -- right after time or its -p, then the timed pipeline.
        plain = reading("git checkout main")
        self.assertEqual(["git"], [call.program for call in plain.calls])
        for prefix in ("time", "time -p", "time --", "time -p --", "! time --", "time -- time -p --", "time -- !"):
            with self.subTest(prefix=prefix):
                read = reading(f"{prefix} git checkout main")
                self.assertEqual((plain.calls, []), (read.calls, read.unread))
                assigned = " ".join(reading(f"{prefix} XDG_CONFIG_HOME=/x git status").unread)
                self.assertIn(hub_guard.NOT_READ["construct assignment before git"], assigned)

    def test_the_constructs_the_grammar_reads_without_a_word_are_not_read(self) -> None:
        for entry, command in (
            ("construct function definition", "f() { :; }"),
            ("construct arithmetic command", "(( x = 1 ))"),
            ("construct arithmetic for", "for (( i = 0; i < 1; i++ )); do :; done"),
            ("construct element assignment", "a[0]=x"),
            ("construct assignment before git", "XDG_CONFIG_HOME=/x git status"),
        ):
            with self.subTest(entry=entry):
                self.assertIn(hub_guard.NOT_READ[entry], " ".join(reading(command).unread))


class ShellOptionTests(unittest.TestCase):
    """Each option set and shopt list is decided by its table, from every source the guard reads: set, shopt, and the
    SHELLOPTS and BASHOPTS the environment hands the shell."""

    def test_every_option_bash_lists_is_in_its_table(self) -> None:
        self.assertEqual(set(), set(listed("set -o")) - set(hub_guard.SET_OPTIONS))
        self.assertEqual(set(), set(listed("shopt")) - set(hub_guard.SHOPT_OPTIONS))

    def test_each_refused_state_has_a_reason_and_each_reason_a_refused_state(self) -> None:
        refused_set = {name for name, (_, state) in hub_guard.SET_OPTIONS.items() if state}
        refused_shopt = {name for name, state in hub_guard.SHOPT_OPTIONS.items() if state}
        self.assertEqual(refused_set, not_read("set -o"))
        self.assertEqual(refused_shopt, not_read("shopt"))

    def test_each_set_option_is_read_by_its_table_from_every_source(self) -> None:
        for name, (letter, state) in hub_guard.SET_OPTIONS.items():
            for on in (True, False):
                refused = state == ("on" if on else "off")
                sign = "-" if on else "+"
                sources = [f"set {sign}o {name}", f"shopt {'-s' if on else '-u'} -o {name}"]
                if letter:
                    sources.append(f"set {sign}{letter}")
                for source in sources:
                    with self.subTest(source=source):
                        self.assertEqual(refused, bool(reading(source).unread))
            with self.subTest(environment=name):
                self.assertEqual(state == "on", bool(reading("true", {"SHELLOPTS": f"hashall:{name}"}).unread))

    def test_each_shopt_option_is_read_by_its_table_from_every_source(self) -> None:
        for name, state in hub_guard.SHOPT_OPTIONS.items():
            for on in (True, False):
                source = f"shopt {'-s' if on else '-u'} {name}"
                with self.subTest(source=source):
                    self.assertEqual(state == ("on" if on else "off"), bool(reading(source).unread))
            with self.subTest(environment=name):
                self.assertEqual(state == "on", bool(reading("true", {"BASHOPTS": name}).unread))

    def test_an_option_neither_table_names_is_not_read(self) -> None:
        for command, environment in (
            ("set -o frobnicate", None),
            ("set -Z", None),
            ("shopt -s frobnicate", None),
            ("set -o $x", None),
            ("set $x", None),
            ("set -e $x", None),
            ("shopt -s $x", None),
            ("shopt -x nullglob", None),
            ("true", {"SHELLOPTS": "frobnicate"}),
            ("true", {"BASHOPTS": "frobnicate"}),
            ("true", {"POSIXLY_CORRECT": ""}),
            ("true", {"BASH_COMPAT": "50"}),
        ):
            with self.subTest(command=command, environment=environment):
                self.assertTrue(reading(command, environment).unread)
        self.assertEqual([], reading("set -euo pipefail; set -- a b; set; shopt -p; shopt -q extglob").unread)
        for command, reason in (
            ("set $x", "set is given a word the text does not settle"),
            ("set -e $x", "set is given a word the text does not settle"),
            ("shopt -s $x", "shopt is given a word the text does not settle"),
            ("shopt -x nullglob", "shopt's option -x, which the guard does not read"),
        ):
            with self.subTest(command=command):
                self.assertIn(reason, " ".join(reading(command).unread))


class VariableTests(unittest.TestCase):
    def test_every_variable_bash_sets_is_in_its_table(self) -> None:
        self.assertEqual(set(), set(listed("compgen -v")) - set(hub_guard.BASH_VARIABLES))

    def test_each_variable_is_assigned_as_its_table_says(self) -> None:
        for name, reason in hub_guard.BASH_VARIABLES.items():
            with self.subTest(name=name):
                self.assertEqual(bool(reason), bool(reading(f"{name}=x; true").unread))
                self.assertEqual(bool(reason), bool(reading(f"{name}=x ls").unread))
        for command in ("x=1; true", "x=1 ls", "for x in a; do :; done", "read -r x", "echo ${x:=1}"):
            with self.subTest(command=command):
                self.assertEqual([], reading(command).unread)

    def test_a_name_git_reads_or_the_environment_holds_is_not_assigned(self) -> None:
        for command, environment in (
            ("GIT_DIR=x ls", None),
            ("GIT_DIR=x; true", None),
            ("EDITOR=x; true", {"EDITOR": "vi"}),
            ("for EDITOR in a; do :; done", {"EDITOR": "vi"}),
            ("echo ${EDITOR:=x}", {"EDITOR": "vi"}),
        ):
            with self.subTest(command=command):
                self.assertTrue(reading(command, environment).unread)
        self.assertEqual([], reading("EDITOR=x ls", {"EDITOR": "vi"}).unread)


# How a runner's command is written where it is no plain word after the runner, with {command} standing for it.
RUNNER_FORMS = {
    "bash": "bash -c '{command}'",
    "pwsh": "pwsh -NoProfile -Command '{command}'",
    # The program time, which Bash runs only where time is not a command's first word, as after an assignment.
    "time": "x=1 time {command}",
    # The operand each takes before its command: a duration, a priority, a CPU mask, and a new root.
    "timeout": "timeout 5 {command}",
    "chrt": "chrt 10 {command}",
    "taskset": "taskset 3 {command}",
    "chroot": "chroot /srv {command}",
}


class RunnerTests(unittest.TestCase):
    """The runner rule, a row per RUNNERS entry for git and for a command that is not git: while a hub is guarded, a
    runner whose command is or may be git is refused, and one around any other command is understood, but for the
    runners RUNNERS_ALWAYS_REFUSED names."""

    def test_each_runner_is_refused_around_git_and_understood_around_any_other_command_it_reads(self) -> None:
        environment = {"PATH": os.environ.get("PATH", "")}
        self.assertLessEqual(hub_guard.RUNNERS_ALWAYS_REFUSED, set(hub_guard.RUNNERS))
        for runner, reason in hub_guard.RUNNERS.items():
            form = RUNNER_FORMS.get(runner, f"{runner} {{command}}")
            self.assertGreater(len(reason.split()), 2)
            for command, refused in (
                (form.format(command="git status"), True),
                (form.format(command="/usr/bin/git log"), True),
                (form.format(command="ls -la"), runner in hub_guard.RUNNERS_ALWAYS_REFUSED),
            ):
                with self.subTest(runner=runner, command=command):
                    self.assertEqual(refused, bool(reading(command, environment).unread))
        for command in ("some-tool x", "./tools/x.sh", "python -B x.py", "node x.js", "npm test", "make all"):
            with self.subTest(command=command):
                self.assertEqual([], reading(command).unread)

    def test_a_command_that_may_be_git_through_a_runner_is_refused(self) -> None:
        for command in (
            "timeout 5 $tool status",
            "nice timeout 5 ls",
            "env GIT_DIR=/x ls",
            "XDG_CONFIG_HOME=/x timeout 5 git co",
            "bash -c 'git status && ls'",
            "bash -lc 'ls'",
            "bash script.sh",
            "xargs -I{} cp {} d",
            "xargs -i cp {} d",
            "xargs --replace=x cp x d",
            "echo git | xargs --replac=X X status",
            "echo git | xargs -0IX X status",
            "env -S 'ls -la'",
            "env --split-string=ls",
            "env --split='git status'",
        ):
            with self.subTest(command=command):
                self.assertTrue(reading(command).unread)
        self.assertEqual([], reading("bash -c 'ls && echo hi'").unread)
        self.assertEqual([], reading("echo x | xargs -0 grep -n x").unread)

    def test_a_program_with_an_option_that_runs_a_command_is_refused_in_every_spelling(self) -> None:
        # tests/tools/test_prefix_programs.py holds every option each program lists; these are the forms reviews found.
        for program in hub_guard.PROGRAM_OPTIONS:
            with self.subTest(program=program, word="unsettled"):
                self.assertTrue(reading(f"{program} $x y").unread)
        for command in (
            "find . -exec ls {} +",
            "find . -execdir ls {} ;",
            "find . -delete",
            "tar xIf a.tar",
            "tar -xIf a.tar",
            "tar -xIgit -f a.tar",
            "tar --to-com=x -xf a.tar",
            "tar --use-compress-prog=x -cf a.tar d",
            "rg --pre=x pattern",
            "rg --hostname-bin=x pattern",
            "sort --compress=x f",
        ):
            with self.subTest(command=command):
                self.assertTrue(reading(command).unread)
        for command in ("find . -name '*.py'", "tar -czf a.tar d", "tar tf a.tar", "rg --pretty x", "sort -r f"):
            with self.subTest(command=command):
                self.assertEqual([], reading(command).unread)

    def test_a_variable_a_program_reads_options_from_is_refused_wherever_it_is_set(self) -> None:
        for program, variable in sorted(
            (program, variable) for program, variables in hub_guard.PROGRAM_VARIABLES.items() for variable in variables
        ):
            for command, environment in (
                (f"{variable}=x {program} -v y", {}),
                (f"env {variable}=x {program} -v y", {}),
                (f"env -i {variable}=x {program} -v y", {}),
                (f"env -u X {variable}=x {program} -v y", {}),
                (f"env --unset X {variable}=x {program} -v y", {}),
                (f"env -u X -- {variable}=x {program} -v y", {}),
                (f"sudo {variable}=x {program} -v y", {}),
                (f"{variable}=x timeout 5 {program} -v y", {}),
                (f"{variable}=x bash -c '{program} -v y'", {}),
                (f"{variable.lower()}=x bash -c '{program} -v y'", {}),
                (f"bash -c \"{variable}=x bash -c '{program} -v y'\"", {}),
                (f'bash -c "bash -c \\"{variable}=x bash -c \'{program} -v y\'\\""', {}),
                (f"{variable}=x pwsh -Command '{program} -v y'", {}),
                (f"{program} -v y", {variable: "x"}),
                (f"{program} -v y", {variable.lower(): "x"}),
                (f"nice {program} -v y", {variable: "x"}),
            ):
                with self.subTest(command=command, environment=environment):
                    self.assertIn(variable, " ".join(reading(command, environment).unread))
            with self.subTest(program=program, variable="unset"):
                self.assertEqual([], reading(f"OTHER=x {program} -v y").unread)
                self.assertEqual([], reading(f"env OTHER=x {program} -v y").unread)
        self.assertEqual({"tar": {"TAR_OPTIONS", "TAPE"}, "rg": {"RIPGREP_CONFIG_PATH"}}, hub_guard.PROGRAM_VARIABLES)

    def test_a_command_s_name_is_read_after_either_separator_whatever_the_host_s_paths(self) -> None:
        rows = {
            "C:\\WINDOWS\\SysWOW64\\SORT.EXE": "sort",
            "C:/Windows/System32/find.exe": "find",
            "/usr/bin/git": "git",
            "..\\tools\\gh.exe": "gh",
            "timeout": "timeout",
        }
        # PurePosixPath reads a path as a Linux host does, where a backslash separates nothing.
        for host in (pathlib.PurePosixPath, pathlib.PureWindowsPath):
            with mock.patch.object(hub_guard, "Path", host):
                for word, name in rows.items():
                    with self.subTest(host=host.__name__, word=word):
                        self.assertEqual(name, hub_guard.command_name(word))

    def test_option_refusal_is_the_one_reading_of_a_program_s_words(self) -> None:
        xargs = hub_guard.RUNNER_OPTIONS["xargs"]
        self.assertEqual(("-0IX", 0), (option_reader.option_refusal(xargs, ["-0IX", "X"]).refused, 0))
        safe = option_reader.option_refusal(xargs, ["-0rn1", "grep", "-n"])
        self.assertEqual((None, 1), (safe.refused, safe.stop))
        env = option_reader.option_refusal(hub_guard.RUNNER_OPTIONS["env"], ["-u", "X", "Y=1", "ls"])
        self.assertEqual(([("Y", "1")], 3), (env.assigned, env.stop))
        tar = option_reader.option_refusal(hub_guard.PROGRAM_OPTIONS["tar"], ["-tf", "a", "--force-local", "--", "-x"])
        self.assertEqual((None, [("-f", "a")], ["--force-local"]), (tar.refused, tar.values, tar.longs))

    def test_gh_a_runner_starts_is_read_where_the_runner_s_options_end(self) -> None:
        for command in (
            "env -u gh gh pr checkout 1",
            "sudo -u gh gh pr checkout 1",
            "sudo -g gh gh repo sync",
            "timeout -s gh 60 gh pr checkout 1",
        ):
            with self.subTest(command=command):
                read = reading(command)
                self.assertEqual([], read.unread)
                self.assertEqual(["gh"], [call.program for call in read.calls])
        # xargs adds words from its input to gh's, which may make any subcommand a checkout.
        for command in (
            "echo checkout 1 | xargs gh pr",
            "echo x | xargs -E gh gh pr checkout 1",
            "xargs -0 gh pr view",
        ):
            with self.subTest(command=command):
                self.assertIn("words from its input", " ".join(reading(command).unread))
        # gh among the command's own words is not the command.
        self.assertEqual([], reading("timeout 60 echo gh pr checkout 1").calls)

    def test_gh_behind_chroot_is_read_where_the_text_does_not_settle_and_behind_timeout_where_it_runs(self) -> None:
        unsettled = reading("chroot /srv gh pr checkout 1")
        self.assertEqual([("gh", None)], [(call.program, call.place.start) for call in unsettled.calls])
        placed = reading("timeout 60 gh pr checkout 1")
        self.assertEqual([("gh", START)], [(call.program, call.place.start) for call in placed.calls])

    def test_env_s_assignments_after_a_valued_option_reach_the_command_s_judgment(self) -> None:
        # GIT_DIR after env's -u X is still an assignment before the command, as one before env is.
        for command in ("env GIT_DIR=/x ls", "env -u X GIT_DIR=/x ls", "env --unset X -i GIT_DIR=/x ls"):
            with self.subTest(command=command):
                self.assertIn("GIT_DIR", " ".join(reading(command).unread))

    def test_a_not_found_handler_the_environment_hands_is_refused_whatever_runs(self) -> None:
        # Bash runs command_not_found_handle for any command whose program it does not find.
        environment = {"BASH_FUNC_command_not_found_handle%%": "() { git checkout -b x; }"}
        self.assertTrue(reading("nosuchtool", environment).unread)
        self.assertEqual([], reading("nosuchtool").unread)


class NestingTests(unittest.TestCase):
    def test_substitutions_nested_past_the_limit_are_refused_and_to_it_read(self) -> None:
        def nested(depth: int) -> str:
            return "echo " + "$(echo " * depth + "hi" + ")" * depth

        reason = f"substitutions nest more than {hub_guard.NESTING_LIMIT} deep"
        self.assertNotIn(reason, " ".join(reading(nested(hub_guard.NESTING_LIMIT)).unread))
        self.assertIn(reason, " ".join(reading(nested(hub_guard.NESTING_LIMIT + 1)).unread))

    def test_sh_is_refused_whatever_it_is_given(self) -> None:
        # sh runs in POSIX mode, where an assignment before a special builtin outlives it.
        for command in ("sh -c 'echo x'", "sh -c 'TMP=/x :; echo x > \"$TMP/f\"'", "sh script.sh", "sh"):
            with self.subTest(command=command):
                self.assertIn(hub_guard.RUNNERS["sh"], " ".join(reading(command).unread))


class RedirectionTests(unittest.TestCase):
    def test_a_redirection_naming_its_descriptor_by_a_variable_is_refused_in_every_operator(self) -> None:
        # Bash assigns the descriptor it opens, or closes, to the variable {NAME} names, which may be any.
        reason = hub_guard.NOT_READ["construct named descriptor"]
        # &> and &>> take no descriptor, so a word before them is an argument, which assigns nothing.
        operators = sorted(bash_syntax.REDIRECTIONS - bash_syntax.HERE_DOCUMENTS - {"&>", "&>>"})
        for operator in operators:
            target = "-" if operator in {">&", "<&"} else "/dev/null"
            command = f": {{HOME}}{operator}{target}; cd"
            with self.subTest(operator=operator):
                self.assertIn(reason, " ".join(reading(command).unread))
        for document in sorted(bash_syntax.HERE_DOCUMENTS):
            with self.subTest(operator=document):
                self.assertIn(reason, " ".join(reading(f"cat {{fd}}{document}'EOF'\nx\nEOF").unread))
        self.assertEqual([], reading("echo x 2>&1 >/dev/null 3>&-").unread)


class ExpansionTests(unittest.TestCase):
    def test_every_expansion_form_is_understood_or_not_read_and_has_a_sample(self) -> None:
        refused = not_read("expansion")
        self.assertEqual(set(), set(hub_guard.EXPANSIONS_UNDERSTOOD) & refused)
        self.assertEqual(set(EXPANSION_SAMPLES), set(hub_guard.EXPANSIONS_UNDERSTOOD) | refused)

    def test_each_form_is_read_as_its_table_says_in_an_argument_and_never_settles_a_command_name(self) -> None:
        bash_agreement.assert_reader_agrees(
            [f"echo {sample}" for form, sample in EXPANSION_SAMPLES.items() if not form.startswith(("${ ", "${|"))]
        )
        for form, sample in EXPANSION_SAMPLES.items():
            with self.subTest(form=form):
                command = f"x=1 {sample}" if form == "NAME=( )" else f"echo {sample}"
                self.assertEqual(form in hub_guard.EXPANSIONS_UNDERSTOOD, not reading(command).unread)
                if form in hub_guard.EXPANSIONS_UNDERSTOOD and form not in {"~", "$'"}:
                    self.assertTrue(reading(f"{sample} status").unread, "a command name it leaves unsettled")

    def test_an_expansion_in_an_unquoted_here_document_is_read_and_in_a_quoted_one_is_data(self) -> None:
        self.assertTrue(reading("cat <<EOF\n$((1 + 2))\nEOF").unread)
        self.assertTrue(reading("cat <<EOF\n$[1 + 2]\nEOF").unread)
        self.assertEqual([], reading("cat <<'EOF'\n$[1 + 2]\nEOF").unread)
        self.assertTrue(reading("cat <<EOF\n${ date; }\nEOF").unread)
        self.assertTrue(reading("cat <<EOF\n${PATH:=x}\nEOF").unread)
        self.assertEqual([], reading("cat <<'EOF'\n$((1 + 2)) ${ date; }\nEOF").unread)
        self.assertEqual([], reading("cat <<EOF\n$x $(date) ${x:-y}\nEOF").unread)

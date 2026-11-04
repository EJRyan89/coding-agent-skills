"""The hub guard's Bash catalogues, each held to the list Bash gives of it: every builtin (`compgen -b`), reserved word
(`compgen -k`), `set -o` option, `shopt` option, and variable (`compgen -v`) Bash lists is understood or in NOT_READ,
and the rows below are generated from those tables, so an entry a later Bash adds fails here until a row decides it."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement

from deployer import platform_support
from tools import bash_syntax, hub_guard

START = Path(__file__).resolve().parent


def reading(command: str, environment: dict[str, str] | None = None) -> hub_guard.Reading:
    """The guard's reading of a Bash command, as data: nothing is run."""
    result = hub_guard.Reading(environment or {}, command)
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


# Each form of a runner whose command the guard reads, with {command} standing for that command: the rows below are
# generated from it, so a runner that gains a reader gains its rows.
RUNNER_FORMS = {
    "timeout": "timeout 5 {command}",
    "nice": "nice -n 5 {command}",
    "nohup": "nohup {command}",
    "stdbuf": "stdbuf -oL {command}",
    "xargs": "xargs {command}",
    "env": "env {command}",
    "bash": "bash -c '{command}'",
    "sh": "sh -c '{command}'",
    "pwsh": "pwsh -NoProfile -Command '{command}'",
    "find": "find . -exec {command} {{}} +",
}


class RunnerTests(unittest.TestCase):
    def test_every_runner_whose_command_the_guard_reads_has_a_form(self) -> None:
        read = {*hub_guard.RUNNER_OPTIONS, "env", "bash", "sh", "pwsh", "find"}
        self.assertEqual(read, set(RUNNER_FORMS))

    def test_an_assignment_before_a_runner_is_judged_against_the_command_it_starts(self) -> None:
        # A runner is transparent: what reaches git through one is judged as an assignment before git.
        environment = {"PATH": os.environ.get("PATH", "")}
        refused = hub_guard.NOT_READ["construct assignment before git"]
        for runner, form in RUNNER_FORMS.items():
            for command, expected in (
                (f"XDG_CONFIG_HOME=/x {form.format(command='git co')}", refused),
                (f"EDITOR=vi {form.format(command='git commit -m x')}", refused),
                (f"env XDG_CONFIG_HOME=/x {form.format(command='git status')}", refused),
                (f"XDG_CONFIG_HOME=/x bash -c '{form.format(command='git status')}'", refused),
                (f"LC_ALL=C {form.format(command='git log')}", None),
                # pwsh's commands are read apart, so what reaches them is judged as an assignment before git.
                (f"PYTHONUTF8=1 {form.format(command='python -B x.py')}", refused if runner == "pwsh" else None),
            ):
                if runner in {"bash", "sh", "pwsh"} and command.count("'") > 2:
                    continue  # a quoted form inside another quoted form is no command a session writes
                with self.subTest(runner=runner, command=command):
                    unread = " ".join(reading(command, environment).unread)
                    if expected is None:
                        self.assertEqual("", unread)
                    else:
                        self.assertIn(expected, unread)

    def test_each_runner_is_refused_in_a_form_the_guard_does_not_read_and_any_other_program_is_read(self) -> None:
        for name, reason in hub_guard.RUNNERS.items():
            with self.subTest(runner=name):
                self.assertGreater(len(reason.split()), 2)
                self.assertTrue(reading(f"x=1 {name} --frobnicate git status").unread)
        for command in ("some-tool x", "./tools/x.sh", "python -B x.py", "node x.js", "npm test", "make all"):
            with self.subTest(command=command):
                self.assertEqual([], reading(command).unread)

    def test_a_program_with_an_option_that_runs_a_command_is_refused_on_that_option(self) -> None:
        for program, options in hub_guard.COMMAND_OPTIONS.items():
            for option in sorted(options - hub_guard.FIND_COMMANDS):
                with self.subTest(program=program, option=option):
                    self.assertTrue(reading(f"{program} {option} x y").unread)
            with self.subTest(program=program, word="unsettled"):
                self.assertTrue(reading(f"{program} $x y").unread)


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
        self.assertTrue(reading("cat <<EOF\n${ date; }\nEOF").unread)
        self.assertTrue(reading("cat <<EOF\n${PATH:=x}\nEOF").unread)
        self.assertEqual([], reading("cat <<'EOF'\n$((1 + 2)) ${ date; }\nEOF").unread)
        self.assertEqual([], reading("cat <<EOF\n$x $(date) ${x:-y}\nEOF").unread)

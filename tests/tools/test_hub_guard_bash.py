"""Regression suite for tools/hub_guard.py's Bash reader: the builtins that set a variable, read, printf -v, and
getopts, and assignment through a name reference, which declare -n, local -n, and typeset -n make.

It starts no program and holds no fixture, so it runs in under a second, apart from tests/tools/test_hub_guard.py,
whose suites start git and pwsh; tests/tools/test_hub_guard_shells.py holds these readings to what Bash does.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import hub_guard

START = Path(__file__).resolve().parent
UNSETTLED = "unsettled"
UNSET = "unset"


def git_dir_reaching_git(command: str) -> Path | str:
    """The repository the one git call that ends the command acts on, as the Bash reader settles it from GIT_DIR,
    started with GIT_DIR exported as `before`: UNSETTLED when the text does not settle it, and UNSET when no GIT_DIR
    reaches the call."""
    text = f"export GIT_DIR=before; {command}; git status"
    reading = hub_guard.Reading({}, text)
    hub_guard.read_bash(text, START, reading, 0, {})
    [call] = reading.calls
    if not call.place.repository_named:
        return UNSET
    return UNSETTLED if call.place.repository is None else call.place.repository


class BuiltinAssignmentTests(unittest.TestCase):
    """read, printf -v, and getopts set the variables they name, through any name reference, and the guard leaves each
    such variable unsettled."""

    def assert_reading(self, cases: tuple[tuple[str, Path | str], ...]) -> None:
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(expected, git_dir_reaching_git(command))

    def test_read_leaves_each_name_it_is_given_unsettled_whatever_its_options(self) -> None:
        self.assert_reading(
            (
                ("read GIT_DIR <<< after", UNSETTLED),
                ("read -r GIT_DIR <<< after", UNSETTLED),
                ("read other GIT_DIR <<< 'one two'", UNSETTLED),
                ("read -a GIT_DIR <<< after", UNSETTLED),
                ("read -ra GIT_DIR <<< after", UNSETTLED),
                ("read -aGIT_DIR <<< after", UNSETTLED),
                ("read -d , GIT_DIR <<< after,", UNSETTLED),
                ("read -rd, other GIT_DIR <<< 'one two,'", UNSETTLED),
                ("read -d '' GIT_DIR <<< after", UNSETTLED),
                ("read -p prompt GIT_DIR <<< after", UNSETTLED),
                ("read -t 1 -n 5 -N 5 -i text -u 0 GIT_DIR <<< after", UNSETTLED),
                ("read -- GIT_DIR <<< after", UNSETTLED),
                ("read 'GIT_DIR[0]' <<< after", UNSETTLED),
                ("builtin read GIT_DIR <<< after", UNSETTLED),
                ("command read GIT_DIR <<< after", UNSETTLED),
                # A name the text does not settle may be any variable.
                ('read "$name" <<< after', UNSETTLED),
                ('read -a "$name" <<< after', UNSETTLED),
                ('read "$option" other <<< after', UNSETTLED),
                # An option's value the text does not settle may split into names too.
                ("read -p $prompt other <<< after", UNSETTLED),
                ("read -d $delimiter other <<< after", UNSETTLED),
                ("read -u $descriptor other <<< after", UNSETTLED),
                ("read other $more <<< after", UNSETTLED),
                ("read - -p GIT_DIR <<< after", UNSETTLED),
            )
        )

    def test_read_leaves_a_variable_it_does_not_name_as_it_was(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("read <<< after", before),
                ("read other <<< after", before),
                ("read -p GIT_DIR other <<< after", before),
                ("read -d GIT_DIR other <<< after", before),
                ("read -rp GIT_DIR other <<< after", before),
                ("read -a other <<< after", before),
                # Each option that takes a value takes the next word, which is then no name.
                ("read -d GIT_DIR other <<< after", before),
                ("read -i GIT_DIR other <<< after", before),
                ("read -n GIT_DIR other <<< after", before),
                ("read -N GIT_DIR other <<< after", before),
                ("read -t GIT_DIR other <<< after", before),
                ("read -u GIT_DIR other <<< after", before),
                ("read -p", before),
                # -- ends the options, and a lone - is the first name.
                ("read -- other <<< after", before),
                ("read -p GIT_DIR -- other <<< after", before),
                ("read -- -aGIT_DIR <<< after", before),
                ("export -n GIT_DIR; read GIT_DIR <<< after", UNSET),
            )
        )

    def test_printf_v_leaves_its_name_unsettled_and_printf_without_it_sets_nothing(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("printf -v GIT_DIR %s after", UNSETTLED),
                ("printf -vGIT_DIR %s after", UNSETTLED),
                ("printf -v other -v GIT_DIR %s after", UNSETTLED),
                ("printf -v 'GIT_DIR[0]' %s after", UNSETTLED),
                ("printf -v GIT_DIR -- %s after", UNSETTLED),
                ("builtin printf -v GIT_DIR %s after", UNSETTLED),
                ('printf "$option" GIT_DIR %s after', UNSETTLED),
                ('printf -v "$name" %s after', UNSETTLED),
                ("printf -- -v GIT_DIR", before),
                ("printf %s -v GIT_DIR", before),
                ("printf -v other %s GIT_DIR", before),
                ("printf -x GIT_DIR", before),
                ("printf -v", before),
                ("printf -vother", before),
                # Only the name after -v is set; the format after it is not.
                ("printf -v other GIT_DIR", before),
                # The program printf sets no variable of the shell.
                ("env printf -v GIT_DIR %s after", before),
            )
        )

    def test_mapfile_and_readarray_leave_the_array_they_name_unsettled(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("mapfile GIT_DIR < /dev/null", UNSETTLED),
                ("readarray GIT_DIR < /dev/null", UNSETTLED),
                ("mapfile -t GIT_DIR < /dev/null", UNSETTLED),
                ("mapfile -d , -n 2 -O 1 -s 0 -u 0 GIT_DIR < /dev/null", UNSETTLED),
                ("mapfile -td, GIT_DIR < /dev/null", UNSETTLED),
                ("mapfile -- GIT_DIR < /dev/null", UNSETTLED),
                ("declare -n ref=GIT_DIR; mapfile ref < /dev/null", UNSETTLED),
                ('mapfile "$name" < /dev/null', UNSETTLED),
                ("mapfile -n $count other < /dev/null", UNSETTLED),
                ("mapfile < /dev/null", before),
                ("mapfile other < /dev/null", before),
                ("mapfile -u 0 other GIT_DIR < /dev/null", before),
                # The array a bare mapfile, or a bare read, sets is MAPFILE, or REPLY, through any reference.
                ("declare -n MAPFILE=GIT_DIR; mapfile < /dev/null", UNSETTLED),
                ("declare -n REPLY=GIT_DIR; read <<< after", UNSETTLED),
            )
        )

    def test_a_mapfile_callback_is_a_command_the_guard_does_not_read(self) -> None:
        text = "mapfile -C 'git commit' -c 1 lines < /dev/null"
        reading = hub_guard.Reading({}, text)
        hub_guard.read_bash(text, START, reading, 0, {})
        self.assertEqual(["could not read the callback mapfile runs"], reading.unread)
        self.assertEqual(UNSETTLED, git_dir_reaching_git("readarray -C callback other < /dev/null"))

    def test_wait_p_leaves_its_name_unsettled(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("wait -p GIT_DIR", UNSETTLED),
                ("wait -n -p GIT_DIR", UNSETTLED),
                ("wait -fpGIT_DIR", UNSETTLED),
                ("declare -n ref=GIT_DIR; wait -p ref", UNSETTLED),
                ('wait -p "$name"', UNSETTLED),
                ("wait $jobs", UNSETTLED),
                ("wait", before),
                ("wait -n", before),
                ("wait -p other GIT_DIR", before),
                ("wait -- -p GIT_DIR", before),
            )
        )

    def test_getopts_leaves_its_name_unsettled(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("getopts ab GIT_DIR -a", UNSETTLED),
                ("getopts -- ab GIT_DIR -a", UNSETTLED),
                ('getopts "$options" GIT_DIR -a', UNSETTLED),
                ('getopts ab "$name" -a', UNSETTLED),
                ("getopts ab other -a", before),
                ("getopts ab", before),
                ("getopts ab other GIT_DIR", before),
                ("getopts $options other -a", UNSETTLED),
            )
        )


class NameReferenceTests(unittest.TestCase):
    """A name reference stands for the variable it names: an assignment to it, an expansion of it, and every builtin
    that sets, exports, or unsets it reach that variable."""

    def assert_reading(self, cases: tuple[tuple[str, Path | str], ...]) -> None:
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(expected, git_dir_reaching_git(command))

    def test_an_assignment_through_a_reference_sets_the_variable_it_names(self) -> None:
        after = START / "after"
        self.assert_reading(
            (
                ("declare -n ref=GIT_DIR; ref=after", after),
                ("typeset -n ref=GIT_DIR; ref=after", after),
                ("f() { local -n ref=GIT_DIR; ref=after; }; f", after),
                ("declare -n ref=GIT_DIR; ref=after true", START / "before"),
                ("declare -n ref=GIT_DIR; export ref=after", after),
                ("declare -n ref=GIT_DIR; declare ref=after", after),
                ("declare -n ref=GIT_DIR; readonly ref=after", after),
                ("declare -n ref=GIT_DIR; declare -x ref=after", after),
                # A reference that stands for none names its variable at its first assignment.
                ("declare -n ref; ref=GIT_DIR; ref=after", after),
                ("declare -n ref; declare ref=GIT_DIR; ref=after", after),
                ("declare -n ref; declare ref=GIT_DIR GIT_DIR=after", after),
                ("declare -n ref; ref=GIT_DIR; declare -n ref; ref=after", after),
                # A reference both paths make alike stands for its variable after them.
                ("if [ -n x ]; then declare -n ref=GIT_DIR; else declare -n ref=GIT_DIR; fi; ref=after", after),
                # Declared again without a value, a reference keeps its variable, and a variable becomes a reference
                # to the one its value names.
                ("declare -n ref=GIT_DIR; declare -n ref; ref=after", after),
                ("ref=GIT_DIR; declare -n ref; ref=after", after),
                # A function's reference that was made before the call, and that it does not change, outlives it.
                ("declare -n ref=GIT_DIR; f() { true; }; f; ref=after", after),
                # A chain of references ends at the variable the last one names.
                ("declare -n first=second second=GIT_DIR; first=after", after),
                ("declare -gn ref=GIT_DIR; ref=after", after),
                ("declare -nr ref=GIT_DIR; ref=after", after),
            )
        )

    def test_a_reference_reaches_its_variable_for_one_command_and_for_an_expansion(self) -> None:
        text = 'declare -n ref=GIT_DIR; ref=after git status; git --git-dir="$ref" status'
        reading = hub_guard.Reading({}, text)
        hub_guard.read_bash(text, START, reading, 0, {})
        self.assertEqual([START / "after", None], [call.place.repository for call in reading.calls])
        # The expansion of a reference is its variable's value, here given to --git-dir as a word of its own.
        text = 'GIT_DIR=elsewhere; declare -n ref=GIT_DIR; git --git-dir "$ref" status'
        reading = hub_guard.Reading({}, text)
        hub_guard.read_bash(text, START, reading, 0, {})
        self.assertEqual([START / "elsewhere"], [call.place.repository for call in reading.calls])

    def test_cd_reads_cdpath_through_a_reference_and_where_the_text_does_not_settle_it(self) -> None:
        # tests/tools is a directory below the start, tests, so cd reaches it unless CDPATH may search elsewhere.
        for command, expected in (
            ("cd tools", START),
            ("CDPATH=; cd tools", START),
            ("CDPATH=/elsewhere; cd tools", None),
            ("declare -n CDPATH=other; other=/elsewhere; cd tools", None),
            ("declare -n CDPATH=other; cd tools", START),
            ("declare -n CDPATH; cd tools", None),
            ("read $name <<< /elsewhere; cd tools", None),
            ("declare -n CDPATH=other; other=/elsewhere; cd ./tools", START),
            # Bash searches CDPATH for neither an absolute directory, nor . and .. and those under them.
            ("CDPATH=/elsewhere; cd .", START.parent),
            ("CDPATH=/elsewhere; cd ..", START.parent.parent),
            ("CDPATH=/elsewhere; cd ../tests/tools", START),
            (f"CDPATH=/elsewhere; cd '{START.as_posix()}'", START),
            ("CDPATH=/elsewhere; cd ~", Path.home()),
            # A ~ left after expansion was quoted, and is searched like any other name.
            ("CDPATH=/elsewhere; cd '~'", None),
        ):
            text = f"{command}; git status"
            reading = hub_guard.Reading({}, text)
            hub_guard.read_bash(text, START.parent, reading, 0, {})
            with self.subTest(command=command):
                starts = [None if call.place.start is None else call.place.start.resolve() for call in reading.calls]
                self.assertEqual([None if expected is None else expected.resolve()], starts)

    def test_functions_are_followed_four_calls_deep(self) -> None:
        definitions = "f1() { f2; }; f2() { f3; }; f3() { f4; }; f4() { git --git-dir after status; }; "
        reading = hub_guard.Reading({}, definitions + "f1")
        hub_guard.read_bash(definitions + "f1", START, reading, 0, {})
        self.assertEqual(([START / "after"], []), ([call.place.repository for call in reading.calls], reading.unread))
        deeper = "f0() { f1; }; " + definitions + "f0"
        reading = hub_guard.Reading({}, deeper)
        hub_guard.read_bash(deeper, START, reading, 0, {})
        self.assertEqual([], reading.calls)
        self.assertEqual(["the function f4 nests more than 4 deep"], reading.unread)
        reading = hub_guard.Reading({}, "f() { f; }; f; git status")
        hub_guard.read_bash("f() { f; }; f; git status", START, reading, 0, {})
        self.assertEqual(["the function f nests more than 4 deep"], reading.unread)

    def test_a_builtin_that_sets_through_a_reference_leaves_its_variable_unsettled(self) -> None:
        self.assert_reading(
            (
                ("declare -n ref=GIT_DIR; read ref <<< after", UNSETTLED),
                ("declare -n ref=GIT_DIR; printf -v ref %s after", UNSETTLED),
                ("declare -n ref=GIT_DIR; getopts ab ref -a", UNSETTLED),
                ("declare -n ref=GIT_DIR; ref+=after", UNSETTLED),
                ("declare -n ref=GIT_DIR; ref=(after)", UNSETTLED),
                ("declare -n ref=GIT_DIR; for item in one; do ref=$item; done", UNSETTLED),
            )
        )

    def test_unset_reaches_the_variable_and_unset_n_and_plus_n_end_the_reference(self) -> None:
        before = START / "before"
        self.assert_reading(
            (
                ("declare -n ref=GIT_DIR; unset ref", UNSET),
                ("declare -n ref=GIT_DIR; unset -v ref", UNSET),
                ("declare -n ref=GIT_DIR; export -n ref", UNSET),
                ("declare -n ref=GIT_DIR; unset -n ref; ref=after", before),
                ("declare -n ref=GIT_DIR; declare +n ref; ref=after", before),
                ("declare -n ref=GIT_DIR; declare -n ref=other; ref=after", before),
                # unset through a reference removes its variable and the variable's export.
                ("declare -n ref=GIT_DIR; unset ref; export GIT_DIR", UNSET),
                # An assignment through an exported reference does not export the variable it reaches.
                ("unset GIT_DIR; declare -nx ref=GIT_DIR; ref=after", UNSET),
                ("unset GIT_DIR; declare -n ref=GIT_DIR; export ref; ref=after", START / "after"),
                # A reference as a loop's variable stands for each word in turn and sets none of them.
                ("declare -n ref=GIT_DIR; for ref in one two; do :; done", before),
                ("declare -n ref=GIT_DIR; unset ref; GIT_DIR=after", UNSET),
                ("declare -n ref=other; unset ref", before),
                # unset -n removes the reference itself, with its own value and export.
                ("declare -n GIT_DIR=other; unset -n GIT_DIR", UNSET),
                ("declare -n GIT_DIR=other; unset -n GIT_DIR; GIT_DIR=after", UNSET),
                ("declare -n GIT_DIR=other; unset -n GIT_DIR; export GIT_DIR", UNSET),
                ("declare -n ref; unset -n ref", before),
                # A reference declared again with no value has no value of its own to give a program.
                ("unset GIT_DIR; export GIT_DIR; declare -n GIT_DIR", UNSET),
                # Only declare, typeset, and local make a reference.
                ("readonly -n ref=GIT_DIR; ref=after", before),
                ("export -n ref=GIT_DIR; ref=after", before),
            )
        )

    def test_a_reference_the_text_does_not_settle_may_reach_any_variable(self) -> None:
        self.assert_reading(
            (
                ('declare -n ref="$name"; ref=after', UNSETTLED),
                ("declare -n ref; ref=$(echo GIT_DIR); ref=after", UNSETTLED),
                ("source ./env.sh; declare -n ref; ref=after", UNSETTLED),
                # A reference a function makes is its own, which Bash drops when it returns.
                ("f() { local -n ref=GIT_DIR; }; f; ref=after", UNSETTLED),
                ("f() { declare -n ref=GIT_DIR; }; f; ref=after", UNSETTLED),
                ("declare -n ref=other; f() { local -n ref=GIT_DIR; }; f; ref=after", UNSETTLED),
                ("declare -n ref; for ref in GIT_DIR; do ref=after; done", UNSETTLED),
                ("declare -n ref=other; for ref in GIT_DIR; do ref=after; done", UNSETTLED),
                ("declare -n ref; unset ref", UNSETTLED),
                ("declare -n ref=GIT_DIR; ref=$(echo after)", UNSETTLED),
                ("declare -n ref='GIT_DIR[0]'; ref=after", UNSETTLED),
                ("declare -n ref; ref=after true", UNSETTLED),
                ("[ -n x ] && declare -n ref=GIT_DIR; ref=after", UNSETTLED),
                ("if true; then declare -n ref=GIT_DIR; else declare -n ref=other; fi; ref=after", UNSETTLED),
                ("[ -n x ] && declare -n ref=GIT_DIR; export ref=after", UNSETTLED),
                ("[ -n x ] && declare -n ref=GIT_DIR; unset ref", UNSETTLED),
                ("[ -n x ] && declare -n ref=GIT_DIR; ref=after true", UNSETTLED),
            )
        )

    def test_a_chain_is_followed_eight_references_deep(self) -> None:
        names = [f"r{index}" for index in range(9)]
        eight = " ".join(f"{name}={target}" for name, target in zip(names[1:], [*names[2:], "GIT_DIR"], strict=True))
        nine = " ".join(f"{name}={target}" for name, target in zip(names, [*names[1:], "GIT_DIR"], strict=True))
        self.assertEqual(START / "after", git_dir_reaching_git(f"declare -n {eight}; r1=after"))
        self.assertEqual(UNSETTLED, git_dir_reaching_git(f"declare -n {nine}; r0=after"))

    def test_an_exported_reference_gives_a_program_its_own_value(self) -> None:
        self.assertEqual(START / "other", git_dir_reaching_git("declare -n GIT_DIR=other"))
        self.assertEqual(START / "other", git_dir_reaching_git("unset GIT_DIR; declare -nx GIT_DIR=other"))


def bash_reading(command: str, environment: dict[str, str] | None = None) -> hub_guard.Reading:
    reading = hub_guard.Reading(environment or {}, command)
    hub_guard.read_bash(command, START, reading, 0, {})
    return reading


ALIASES_DEFINED = f"an alias the command defines ({hub_guard.BASH_ALIASES})"


class OptionBundleTests(unittest.TestCase):
    """Each o or O in a bash or set option bundle takes the next word as its value, as Bash reads it."""

    def test_bash_runs_the_word_after_every_option_value_as_its_c_script(self) -> None:
        script = "git commit -m x"
        for words in (
            ["-c", "-o", "pipefail", script],
            ["-c", "-euo", "pipefail", script],
            ["-euxo", "pipefail", "-c", script],
            ["-co", "pipefail", script],
            ["-oc", "pipefail", script],
            ["-oo", "pipefail", "errexit", "-c", script],
            ["-Oc", "extglob", script],
            ["+Oc", "extglob", script],
            ["-c", "+eo", "pipefail", script],
            ["--rcfile", "x", "-c", script],
        ):
            with self.subTest(words=words):
                self.assertEqual([script], hub_guard.bash_script(list(words), [], hub_guard.Shell(None, {}, set())))
                quoted = " ".join(word if word != script else f"'{word}'" for word in words)
                self.assertEqual(["commit"], [call.subcommand for call in bash_reading(f"bash {quoted}").calls])

    def test_set_reads_allexport_wherever_a_bundle_names_it(self) -> None:
        for command, expected in (
            ("set -o allexport", START / "after"),
            ("set -eo allexport", START / "after"),
            ("set -oe allexport", START / "after"),
            ("set -oo pipefail allexport", START / "after"),
            ("set -ao pipefail", START / "after"),
            ("set -o pipefail", UNSET),
            ("set -eo pipefail", UNSET),
            ("set -o allexport; set +eo allexport", UNSET),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, git_dir_reaching_git(f"unset GIT_DIR; {command}; GIT_DIR=after"))


class BashAliasesTests(unittest.TestCase):
    """A name that reaches BASH_ALIASES, by any spelling Bash sets a variable by, defines an alias the command cannot
    be read past."""

    def test_each_spelling_that_sets_bash_aliases_is_an_alias_the_command_defines(self) -> None:
        for definition in (
            "BASH_ALIASES[g]=git",
            "BASH_ALIASES=([g]=git)",
            "BASH_ALIASES+=([g]=git)",
            "declare -A BASH_ALIASES=([g]=git)",
            "typeset -A BASH_ALIASES; BASH_ALIASES[g]=git",
            "declare 'BASH_ALIASES[g]=git'",
            "printf -v 'BASH_ALIASES[g]' git",
            "read 'BASH_ALIASES[g]' <<< git",
            "read -a BASH_ALIASES <<< git",
            "mapfile BASH_ALIASES <<< git",
            "declare -A BASH_ALIASES=([g]=$(command -v git))",
            "declare -n ref=BASH_ALIASES; ref[g]=git",
            "declare -n ref='BASH_ALIASES[g]'; ref=git",
            # Eight references deep, as far as Bash follows a chain.
            "declare -n r1=r2 r2=r3 r3=r4 r4=r5 r5=r6 r6=r7 r7=r8 r8=BASH_ALIASES; r1[g]=git",
            'name=BASH_ALIASES; declare "$name[g]=git"',
            "eval 'BASH_ALIASES[g]=git'",
            "(BASH_ALIASES[g]=git; true)",
            "f() { BASH_ALIASES[g]=git; }; f",
        ):
            command = f"shopt -s expand_aliases; {definition}\ng commit -m x"
            with self.subTest(definition=definition):
                self.assertIn(ALIASES_DEFINED, bash_reading(command).unread)

    def test_text_that_only_names_bash_aliases_defines_no_alias(self) -> None:
        for command in ("echo BASH_ALIASES; git status", "git status BASH_ALIASES", "unset other; git status"):
            with self.subTest(command=command):
                self.assertEqual([], bash_reading(command).unread)


class ExportedFunctionTests(unittest.TestCase):
    """A nested bash or sh defines the functions its parent exported and those its environment hands it."""

    def subcommands(self, command: str, environment: dict[str, str] | None = None) -> list[str | None]:
        return [call.subcommand for call in bash_reading(command, environment).calls]

    def test_a_function_exported_by_each_spelling_runs_in_a_nested_shell(self) -> None:
        for export, start in (
            ("export -f f", "bash -c f"),
            ("export -f f", "sh -c f"),
            ("declare -fx f", "bash -c f"),
            ("declare -f -x f", "bash -c f"),
            ("typeset -xf f", "sh -c f"),
            ("export -f f", "env bash -c f"),
            ("export -f f", "nice bash -c f"),
            ("export -f f", "bash -c 'bash -c f'"),
            ('export -f "$name"', "bash -c f"),
            # Exported on one path only, it may be exported.
            ('[ -n "$x" ] || export -f f', "bash -c f"),
            ("export -f f; unset -f g", "bash -euo pipefail -c f"),
        ):
            command = f"f() {{ git commit -m x; }}; {export}; {start}"
            with self.subTest(command=command):
                self.assertEqual(["commit"], self.subcommands(command))

    def test_a_function_that_is_not_exported_does_not_run_in_a_nested_shell(self) -> None:
        for command in (
            "f() { git commit -m x; }; bash -c f",
            "f() { git commit -m x; }; export -f f; export -fn f; bash -c f",
            "f() { git commit -m x; }; declare -fx f; declare +x -f f; bash -c f",
            "f() { git commit -m x; }; export -f f; unset -f f; bash -c f",
            "f() { git commit -m x; }; declare -f f; bash -c f",
            "f() { git commit -m x; }; export -f g; bash -c f",
            "f() { git commit -m x; }; (export -f f); bash -c f",
        ):
            with self.subTest(command=command):
                self.assertEqual([], self.subcommands(command))

    def test_the_last_definition_is_the_one_a_nested_shell_runs(self) -> None:
        command = "f() { git status; }; export -f f; f() { git commit -m x; }; bash -c f"
        self.assertEqual(["commit"], self.subcommands(command))

    def test_a_function_the_environment_hands_bash_is_defined(self) -> None:
        value = "() {  git commit -m x\n}"
        self.assertEqual(["commit"], self.subcommands(f"env 'BASH_FUNC_f%%={value}' bash -c f"))
        self.assertEqual(["commit"], self.subcommands("f", {"BASH_FUNC_f%%": value}))
        self.assertEqual(["commit"], self.subcommands("bash -c f", {"BASH_FUNC_f%%": value}))
        # Bash imports only a value that starts as a function's does, `() {`, from a name that ends in %%.
        for environment in ({"BASH_FUNC_f%%": "(){ git commit -m x; }"}, {"BASH_FUNC_f": value}):
            with self.subTest(environment=environment):
                self.assertEqual([], self.subcommands("f", environment))

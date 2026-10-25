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
                # The program printf sets no variable of the shell.
                ("env printf -v GIT_DIR %s after", before),
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
        text = 'export GIT_DIR=before; declare -n ref=GIT_DIR; git --git-dir="$ref" status'
        reading = hub_guard.Reading({}, text)
        hub_guard.read_bash(text, START, reading, 0, {})
        self.assertEqual([START / "before"], [call.place.repository for call in reading.calls])

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

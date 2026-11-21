"""Fixture tests for tests/validation/refusal_steps.py.

The policy fails on a deployer refusal built from one line, or from unpacked lines it cannot follow, without its module
saying why, and passes on one that ends with a step, unpacks lines that end with one, is a cancellation, or is
sanctioned beside the code.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from refusal_steps import (
    COVERED,
    REFUSAL,
    UNFOLLOWED,
    _Refusals,
    loose_refusal_assertion_problems,
    refusal_step_problems,
)
from validation_support import write_fixture_tree

SUFFIX = " ends at its ERROR line; add the step to take as its next line, or state why it needs none in STEP_ALLOWED"
UNFOLLOWED_SUFFIX = (
    " unpacks lines this policy cannot follow, such as a parameter, a call's result, or a list changed in place, so "
    "its step may be missing; write a literal step after them, or the step before them, or state why it needs none in "
    "STEP_ALLOWED"
)
NOTHING_CHECKED = (
    "deployer/errors.py defines no DeployError class, so no deployer refusal could be read; the policy checked nothing"
)
ERRORS = (
    "class DeployError(Exception):\n"
    "    def __init__(self, *lines, exit_code=1):\n"
    "        self.lines = lines\n"
    "\n"
    "\n"
    "class Cancelled(DeployError):\n"
    "    pass\n"
)


class RefusalStepFixtures(unittest.TestCase):
    def problems(self, files: dict[str, str], errors: str | None = ERRORS) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {**({"deployer/errors.py": errors} if errors is not None else {}), **files})
            return refusal_step_problems(root)

    def test_a_tree_without_the_refusal_class_fails_rather_than_passing_as_checked(self) -> None:
        refusal = 'from .errors import DeployError\n\n\ndef read():\n    raise DeployError("ERROR: Bare")\n'
        for label, files, errors, expected in (
            ("no deployer", {}, None, []),
            (
                "no errors module",
                {"deployer/read.py": refusal},
                None,
                ["deployer/read.py:5 DeployError in read" + SUFFIX],
            ),
            ("the class renamed", {}, "class Refusal(Exception):\n    pass\n", []),
            ("the class only nested", {}, "def make():\n    class DeployError(Exception):\n        pass\n", []),
        ):
            with self.subTest(label):
                self.assertEqual([*expected, NOTHING_CHECKED], self.problems(files, errors))

    def test_a_file_that_does_not_parse_is_reported_by_name(self) -> None:
        problems = self.problems({"deployer/broken.py": "def broken(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("deployer/broken.py does not parse: "), problems)

    def test_a_refusal_built_from_one_line_fails_however_its_class_is_reached(self) -> None:
        files = {
            "deployer/strayed.py": (
                "from . import errors\n"  # 1
                "from .errors import DeployError as Refusal\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "class Narrower(Refusal):\n"  # 5: a refusal by its base
                "    pass\n"  # 6
                "\n"  # 7
                "\n"  # 8
                "def read(path):\n"  # 9
                '    raise Refusal(f"ERROR: Cannot read {path}")\n'  # 10: through an alias
                "\n"  # 11
                "\n"  # 12
                "class Journal:\n"  # 13
                "    def write(self):\n"  # 14
                '        raise errors.DeployError("ERROR: Cannot write", exit_code=2)\n'  # 15: through its module
                "\n"  # 16
                "\n"  # 17
                "def narrow():\n"  # 18
                '    raise Narrower("ERROR: Narrower")\n'  # 19: a class the deployer derives from it
            ),
            "deploy.py": (
                "from deployer.errors import DeployError\n"
                "\n"
                "\n"
                "def main():\n"
                '    raise DeployError("ERROR: Bare")\n'  # 5: an absolute import
            ),
            # A subclass in another module than the one that raises it.
            "deployer/deeper.py": (
                "from .strayed import Narrower\n"
                "\n"
                "\n"
                "class Deepest(Narrower):\n"
                "    pass\n"
                "\n"
                "\n"
                'FAILED = Deepest("ERROR: Deepest")\n'  # 8: a module-level assignment's scope
            ),
        }
        self.assertEqual(
            [
                "deploy.py:5 DeployError in main" + SUFFIX,
                "deployer/deeper.py:8 Deepest in FAILED" + SUFFIX,
                "deployer/strayed.py:10 DeployError in read" + SUFFIX,
                "deployer/strayed.py:15 DeployError in Journal.write" + SUFFIX,
                "deployer/strayed.py:19 Narrower in narrow" + SUFFIX,
            ],
            self.problems(files),
        )

    def test_a_refusal_with_a_step_unpacked_lines_a_cancellation_or_a_stated_reason_passes(self) -> None:
        files = {
            "deployer/kept.py": (
                'STEP_ALLOWED = {"parse": "read, its one caller, adds the step"}\n'
                "from .errors import Cancelled, DeployError\n"
                "\n"
                "\n"
                "def parse(text):\n"
                '    raise DeployError("ERROR: Malformed")\n'
                "\n"
                "\n"
                "def read(path):\n"
                "    try:\n"
                "        return parse(path)\n"
                "    except DeployError as exc:\n"
                '        raise DeployError(*exc.lines, "Run configure again.") from exc\n'
                "\n"
                "\n"
                "def check(path):\n"
                '    raise DeployError(f"ERROR: {path} is missing", "Put it back, then retry.")\n'
                "\n"
                "\n"
                "def ask():\n"
                '    raise Cancelled("Cancelled; nothing was changed.")\n'
                "\n"
                "\n"
                "def other():\n"
                '    return print("ERROR: not a refusal")\n'
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_unpacked_lines_that_follow_to_one_line_fail(self) -> None:
        files = {
            "deployer/unpacked.py": (
                "from .errors import DeployError\n"  # 1
                "\n"  # 2
                'BARE = ("ERROR: Bare",)\n'  # 3
                "\n"  # 4
                "\n"  # 5
                "def one_line():\n"  # 6
                '    lines = ["ERROR: Bare"]\n'  # 7
                "    raise DeployError(*lines)\n"  # 8: a name bound to a display of one line
                "\n"  # 9
                "\n"  # 10
                "def literal():\n"  # 11
                '    raise DeployError(*["ERROR: Bare"])\n'  # 12: an unpacked display of one line
                "\n"  # 13
                "\n"  # 14
                "def held():\n"  # 15
                "    raise DeployError(*BARE)\n"  # 16: a module-level display of one line
                "\n"  # 17
                "\n"  # 18
                "def empty_step():\n"  # 19
                '    raise DeployError("ERROR: Bare", *())\n'  # 20: an empty unpacking adds no step
            ),
        }
        self.assertEqual(
            [
                "deployer/unpacked.py:8 DeployError in one_line" + SUFFIX,
                "deployer/unpacked.py:12 DeployError in literal" + SUFFIX,
                "deployer/unpacked.py:16 DeployError in held" + SUFFIX,
                "deployer/unpacked.py:20 DeployError in empty_step" + SUFFIX,
            ],
            self.problems(files),
        )

    def test_unpacked_lines_that_end_with_a_step_or_re_raise_a_caught_refusal_pass(self) -> None:
        files = {
            "deployer/followed.py": (
                "from .errors import DeployError\n"
                "\n"
                'REMEDY = ("Run configure again.",)\n'
                "\n"
                "\n"
                "def built(stuck):\n"
                '    lines: list[str] = [*(f"ERROR: {run}" for run in stuck), "Reconcile it by hand."]\n'
                "    raise DeployError(*lines)\n"
                "\n"
                "\n"
                "def nested():\n"
                '    step = ("Retry.",)\n'
                '    raise DeployError(*["ERROR: Nested", *step])\n'
                "\n"
                "\n"
                "def held():\n"
                '    raise DeployError("ERROR: Malformed", *REMEDY)\n'
                "\n"
                "\n"
                "def re_raised():\n"
                "    try:\n"
                "        pass\n"
                "    except (DeployError,) as exc:\n"
                "        raise DeployError(*exc.lines) from exc\n"
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_a_one_line_list_changed_in_place_or_shared_with_another_scope_fails_as_unfollowed(self) -> None:
        # Each list below is displayed with one line; a subscript stored into, any list method that changes it, and a
        # global declaration that lets another scope rebind it each make its display no longer the whole story, so the
        # policy cannot tell where the lines end and refuses the call as one it cannot follow.
        files = {
            "deployer/changed.py": (
                "from .errors import DeployError\n"
                "\n"
                'SHARED = ["ERROR: Shared"]\n'
                "\n"
                "\n"
                "def stored():\n"
                '    lines = ["ERROR: Stored"]\n'
                '    lines[0] = "ERROR: Replaced"\n'
                "    raise DeployError(*lines)\n"
                "\n"
                "\n"
                "def extended():\n"
                '    lines = ["ERROR: Extended"]\n'
                '    lines.extend(["Retry."])\n'
                "    raise DeployError(*lines)\n"
                "\n"
                "\n"
                "def declared():\n"
                "    global SHARED\n"
                "    raise DeployError(*SHARED)\n"
            ),
        }
        self.assertEqual(
            [
                "deployer/changed.py:9 DeployError in stored" + UNFOLLOWED_SUFFIX,
                "deployer/changed.py:15 DeployError in extended" + UNFOLLOWED_SUFFIX,
                "deployer/changed.py:20 DeployError in declared" + UNFOLLOWED_SUFFIX,
            ],
            self.problems(files),
        )

    def test_caught_lines_are_covered_only_from_a_refusal_caught_and_kept_in_the_same_scope(self) -> None:
        refusals = {REFUSAL, "deployer.errors.Cancelled"}
        for label, body, expected in (
            ("caught", "    except DeployError as exc:\n        raise DeployError(*exc.lines)\n", COVERED),
            (
                "rebound",
                "    except DeployError as exc:\n        exc = other()\n        raise DeployError(*exc.lines)\n",
                UNFOLLOWED,
            ),
            ("bare", "    except:\n        raise DeployError(*exc.lines)\n", UNFOLLOWED),
            (
                "nested",
                "    except DeployError as exc:\n        def later():\n            raise DeployError(*exc.lines)\n",
                UNFOLLOWED,
            ),
        ):
            with self.subTest(label):
                source = f"from deployer.errors import DeployError\n\n\ndef read():\n    try:\n        pass\n{body}"
                tree = ast.parse(source)
                call = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DeployError"
                )
                self.assertEqual(expected, _Refusals(tree, "deployer/read.py", refusals).ending(call))

    def test_lines_the_policy_cannot_follow_fail_unless_a_step_is_written_beside_them(self) -> None:
        # Lines known only when the deployer runs may end at the ERROR line, so each refusal ending with them fails.
        files = {
            "deployer/unfollowed.py": (
                "from .errors import DeployError\n"  # 1
                "\n"  # 2
                "\n"  # 3
                "def grown():\n"  # 4
                '    lines = ["ERROR: Grown"]\n'  # 5
                '    lines.append("Retry.")\n'  # 6
                "    raise DeployError(*lines)\n"  # 7: a list appended to
                "\n"  # 8
                "\n"  # 9
                "def rebound(flag):\n"  # 10
                '    lines = ["ERROR: One", "Retry."]\n'  # 11
                "    if flag:\n"  # 12
                '        lines = ["ERROR: Two"]\n'  # 13
                "    raise DeployError(*lines)\n"  # 14: bound twice
                "\n"  # 15
                "\n"  # 16
                "def given(hint):\n"  # 17
                '    raise DeployError("ERROR: Given", *hint)\n'  # 18: a parameter
                "\n"  # 19
                "\n"  # 20
                "def called():\n"  # 21
                '    raise DeployError("ERROR: Called", *steps())\n'  # 22: a call's result
                "\n"  # 23
                "\n"  # 24
                "def wider():\n"  # 25
                "    try:\n"  # 26
                "        pass\n"  # 27
                "    except Exception as exc:\n"  # 28
                "        raise DeployError(*exc.lines) from exc\n"  # 29: not caught as a refusal
                "\n"  # 30
                "\n"  # 31
                "def unpacked_step(more):\n"  # 32
                '    raise DeployError("ERROR: Unpacked", *("Retry.",), *more)\n'  # 33: no written step before more
            ),
        }
        self.assertEqual(
            [
                f"deployer/unfollowed.py:{line} DeployError in {scope}" + UNFOLLOWED_SUFFIX
                for line, scope in (
                    (7, "grown"),
                    (14, "rebound"),
                    (18, "given"),
                    (22, "called"),
                    (29, "wider"),
                    (33, "unpacked_step"),
                )
            ],
            self.problems(files),
        )

    def test_a_caught_refusal_whose_step_its_callers_add_fails_when_its_lines_are_raised_again_alone(self) -> None:
        # config.parse's refusals end without a step, which its STEP_ALLOWED says its callers add, and a cancellation
        # needs none, so a refusal re-built from either's caught lines alone ends at its ERROR line; one caught around
        # a function no allowance excuses was built with its step, and one that adds the step after them has it.
        files = {
            "deployer/config.py": (
                'STEP_ALLOWED = {"parse": "read adds the step to drop the key"}\n'  # 1
                "from .errors import DeployError\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "def parse(text):\n"  # 5
                '    raise DeployError("ERROR: Unknown key")\n'  # 6
            ),
            "deployer/load.py": (
                "from . import config\n"  # 1
                "from .errors import Cancelled, DeployError\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "def wrapped(text):\n"  # 5
                "    try:\n"  # 6
                "        config.parse(text)\n"  # 7
                "    except DeployError as exc:\n"  # 8
                "        raise DeployError(*exc.lines) from exc\n"  # 9: parse's lines, with no step
                "\n"  # 10
                "\n"  # 11
                "def cancelled():\n"  # 12
                "    try:\n"  # 13
                "        ask()\n"  # 14
                "    except Cancelled as exc:\n"  # 15
                "        raise DeployError(*exc.lines) from exc\n"  # 16: a cancellation's one line
                "\n"  # 17
                "\n"  # 18
                "def raised_here():\n"  # 19
                "    try:\n"  # 20
                '        raise Cancelled("Cancelled.")\n'  # 21
                "    except DeployError as exc:\n"  # 22
                "        raise DeployError(*exc.lines) from exc\n"  # 23: the cancellation built in the try
                "\n"  # 24
                "\n"  # 25
                "def stepped(text):\n"  # 26
                "    try:\n"  # 27
                "        config.parse(text)\n"  # 28
                "    except DeployError as exc:\n"  # 29
                '        raise DeployError(*exc.lines, "Run configure again.") from exc\n'  # 30
                "\n"  # 31
                "\n"  # 32
                "def other():\n"  # 33
                "    try:\n"  # 34
                "        check(len([]))\n"  # 35
                "    except DeployError as exc:\n"  # 36
                "        raise DeployError(*exc.lines) from exc\n"  # 37: check's refusal ends with its step
                "\n"  # 38
                "\n"  # 39
                "def check(count):\n"  # 40
                "    if count:\n"  # 41
                '        raise DeployError("ERROR: Counted", "Retry.")\n'  # 42
                "\n"  # 43
                "\n"  # 44
                "def unknown(store):\n"  # 45
                "    try:\n"  # 46
                "        store.save()\n"  # 47
                "    except DeployError as exc:\n"  # 48
                "        raise DeployError(*exc.lines) from exc\n"  # 49: a method of a value, known only at run time
            ),
        }
        self.assertEqual(
            [
                f"deployer/load.py:{line} DeployError in {scope}" + UNFOLLOWED_SUFFIX
                for line, scope in ((9, "wrapped"), (16, "cancelled"), (23, "raised_here"), (49, "unknown"))
            ],
            self.problems(files),
        )

    def test_a_caught_refusal_is_followed_into_each_kind_of_call_and_fails_closed_where_it_cannot_be(self) -> None:
        # One case per branch of the walk: a call rooted in no name, a deployer name with no definition, a class whose
        # __init__ reaches the excused function, one whose __init__ does not, and a class with no __init__ of its own.
        files = {
            "deployer/objects.py": (
                'STEP_ALLOWED = {"excused": "its callers add the step"}\n'  # 1
                "from .config import missing\n"  # 2
                "from .errors import DeployError\n"  # 3
                "\n"  # 4
                "\n"  # 5
                "def excused():\n"  # 6
                '    raise DeployError("ERROR: Excused")\n'  # 7
                "\n"  # 8
                "\n"  # 9
                "class Reaching:\n"  # 10
                "    def __init__(self):\n"  # 11
                "        excused()\n"  # 12
                "\n"  # 13
                "\n"  # 14
                "class Quiet:\n"  # 15
                "    def __init__(self):\n"  # 16
                "        self.count = len([])\n"  # 17
                "\n"  # 18
                "\n"  # 19
                "class Plain:\n"  # 20
                "    count = 0\n"  # 21
                "\n"  # 22
                "\n"  # 23
                "def unnamed():\n"  # 24
                "    try:\n"  # 25
                "        (lambda: excused())()\n"  # 26
                "    except DeployError as exc:\n"  # 27
                "        raise DeployError(*exc.lines) from exc\n"  # 28: a call rooted in a lambda
                "\n"  # 29
                "\n"  # 30
                "def made(factory):\n"  # 31
                "    try:\n"  # 32
                "        factory()()\n"  # 33
                "    except DeployError as exc:\n"  # 34
                "        raise DeployError(*exc.lines) from exc\n"  # 35: a call of a call's result
                "\n"  # 36
                "\n"  # 37
                "def undefined():\n"  # 38
                "    try:\n"  # 39
                "        missing()\n"  # 40
                "    except DeployError as exc:\n"  # 41
                "        raise DeployError(*exc.lines) from exc\n"  # 42: a deployer name with no definition
                "\n"  # 43
                "\n"  # 44
                "def built():\n"  # 45
                "    try:\n"  # 46
                "        Reaching()\n"  # 47
                "    except DeployError as exc:\n"  # 48
                "        raise DeployError(*exc.lines) from exc\n"  # 49: its __init__ reaches excused
                "\n"  # 50
                "\n"  # 51
                "def quiet():\n"  # 52
                "    try:\n"  # 53
                "        Quiet()\n"  # 54
                "    except DeployError as exc:\n"  # 55
                "        raise DeployError(*exc.lines) from exc\n"  # 56: its __init__ reaches nothing excused
                "\n"  # 57
                "\n"  # 58
                "def plain():\n"  # 59
                "    try:\n"  # 60
                "        Plain()\n"  # 61
                "    except DeployError as exc:\n"  # 62
                "        raise DeployError(*exc.lines) from exc\n"  # 63: no __init__ of its own
            ),
            "deployer/config.py": "VALUE = 1\n",
        }
        self.assertEqual(
            [
                f"deployer/objects.py:{line} DeployError in {scope}" + UNFOLLOWED_SUFFIX
                for line, scope in ((28, "unnamed"), (35, "made"), (42, "undefined"), (49, "built"))
            ],
            self.problems(files),
        )

    def test_following_a_caught_refusal_through_recursion_or_a_cycle_ends(self) -> None:
        # Each function is entered once per walk, so a recursive one and two that call each other end: the walk below
        # finds no excused callee and reads the caught lines as built with their step, and one that reaches the
        # excused function after the cycle is still refused.
        files = {
            "deployer/cycle.py": (
                'STEP_ALLOWED = {"excused": "its callers add the step"}\n'  # 1
                "from .errors import DeployError\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "def excused():\n"  # 5
                '    raise DeployError("ERROR: Excused")\n'  # 6
                "\n"  # 7
                "\n"  # 8
                "def ping(count):\n"  # 9
                "    if count:\n"  # 10
                "        pong(count - 1)\n"  # 11
                "        ping(count - 1)\n"  # 12
                "\n"  # 13
                "\n"  # 14
                "def pong(count):\n"  # 15
                "    ping(count)\n"  # 16
                "\n"  # 17
                "\n"  # 18
                "def loop(count):\n"  # 19
                "    pong(count)\n"  # 20
                "    excused()\n"  # 21
                "\n"  # 22
                "\n"  # 23
                "def closed():\n"  # 24
                "    try:\n"  # 25
                "        ping(3)\n"  # 26
                "    except DeployError as exc:\n"  # 27
                "        raise DeployError(*exc.lines) from exc\n"  # 28: the cycle reaches nothing excused
                "\n"  # 29
                "\n"  # 30
                "def reached():\n"  # 31
                "    try:\n"  # 32
                "        loop(3)\n"  # 33
                "    except DeployError as exc:\n"  # 34
                "        raise DeployError(*exc.lines) from exc\n"  # 35: excused after the cycle
            ),
        }
        self.assertEqual(["deployer/cycle.py:35 DeployError in reached" + UNFOLLOWED_SUFFIX], self.problems(files))

    def test_a_caught_refusal_an_excused_function_raises_through_a_helper_fails_when_raised_again_alone(self) -> None:
        # deployer/config.py's load wraps validate_directories, which lets _validate_absolute_directory's refusal,
        # excused because its callers add the step, escape with no handler; with load's step dropped, as below, the
        # policy read the caught lines as built with a step, since the try body named no excused function itself.
        files = {
            "deployer/config.py": (
                'STEP_ALLOWED = {"_validate_absolute_directory": "its callers add the step"}\n'  # 1
                "from .errors import DeployError\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "def _validate_absolute_directory(key, value):\n"  # 5
                '    raise DeployError(f"ERROR: {key} is not absolute")\n'  # 6
                "\n"  # 7
                "\n"  # 8
                "def validate_directories(values):\n"  # 9
                "    for key in values:\n"  # 10
                '        _validate_absolute_directory(key, values.get(key, ""))\n'  # 11
                "\n"  # 12
                "\n"  # 13
                "def load(values):\n"  # 14
                "    try:\n"  # 15
                "        validate_directories(values)\n"  # 16
                "    except DeployError as exc:\n"  # 17
                "        raise DeployError(*exc.lines) from exc\n"  # 18: the step load added, dropped
                "    return values\n"  # 19
                "\n"  # 20
                "\n"  # 21
                "def stepped(values):\n"  # 22
                "    try:\n"  # 23
                "        validate_directories(values)\n"  # 24
                "    except DeployError as exc:\n"  # 25
                '        raise DeployError(*exc.lines, "Run configure to change it.") from exc\n'  # 26
            ),
        }
        self.assertEqual(
            ["deployer/config.py:18 DeployError in load" + UNFOLLOWED_SUFFIX],
            self.problems(files),
        )

    def test_a_one_line_list_handed_to_another_name_or_a_call_fails_as_unfollowed(self) -> None:
        # The list's display shows one line, but the name or the call it is handed to may change it in place, so the
        # display is no longer the whole story; a list only unpacked, indexed, compared, or looped over still is.
        files = {
            "deployer/handed.py": (
                "from .errors import DeployError\n"  # 1
                "\n"  # 2
                "\n"  # 3
                "def aliased():\n"  # 4
                '    lines = ["ERROR: Aliased", "Retry."]\n'  # 5
                "    alias = lines\n"  # 6
                "    alias.pop()\n"  # 7
                "    raise DeployError(*lines)\n"  # 8
                "\n"  # 9
                "\n"  # 10
                "def trimmed():\n"  # 11
                '    lines = ["ERROR: Trimmed", "Retry."]\n'  # 12
                "    trim(lines)\n"  # 13
                "    raise DeployError(*lines)\n"  # 14
                "\n"  # 15
                "\n"  # 16
                "def read():\n"  # 17
                '    lines = ["ERROR: Read", "Retry."]\n'  # 18
                "    first = lines[0]\n"  # 19
                "    for line in lines:\n"  # 20
                "        print(line, first, lines == [], lines.count(line))\n"  # 21
                "    raise DeployError(*lines)\n"  # 22: read, never handed on
                "\n"  # 23
                "\n"  # 24
                "def held_tuple():\n"  # 25
                '    lines = ("ERROR: Tuple", "Retry.")\n'  # 26
                "    keep(lines)\n"  # 27
                "    raise DeployError(*lines)\n"  # 28: a tuple nothing can change
            ),
        }
        self.assertEqual(
            [
                "deployer/handed.py:8 DeployError in aliased" + UNFOLLOWED_SUFFIX,
                "deployer/handed.py:14 DeployError in trimmed" + UNFOLLOWED_SUFFIX,
            ],
            self.problems(files),
        )

    def test_lines_the_policy_cannot_follow_pass_beside_a_written_step_or_a_stated_reason(self) -> None:
        files = {
            "deployer/beside.py": (
                'STEP_ALLOWED = {"reported": "printed, never raised, and its caller prints the steps after it"}\n'
                "from .errors import DeployError\n"
                "\n"
                "\n"
                "def after(hint):\n"
                '    raise DeployError("ERROR: After", *hint, "Retry.")\n'
                "\n"
                "\n"
                "def before(step, *more):\n"
                '    raise DeployError("ERROR: Before", step, *more)\n'
                "\n"
                "\n"
                "def reported(more):\n"
                '    return DeployError("ERROR: Reported", *more)\n'
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_an_allowance_no_longer_needed_or_malformed_fails(self) -> None:
        files = {
            "deployer/gone.py": (
                'STEP_ALLOWED = {"parse": "read adds the step"}\n'
                "from .errors import DeployError\n"
                "\n"
                "\n"
                "def parse(text):\n"
                '    raise DeployError("ERROR: Malformed", "Run configure again.")\n'
            ),
            "deployer/unexplained.py": 'STEP_ALLOWED = {"parse": ""}\n',
        }
        self.assertEqual(
            [
                "deployer/gone.py: STEP_ALLOWED allows parse, which no longer builds a refusal from one line or from "
                "lines this policy cannot follow",
                "deployer/unexplained.py: STEP_ALLOWED must map each scope to the reason its refusals need no step",
            ],
            self.problems(files),
        )


HARNESS = (
    "def assert_refusal(lines, error, *steps):\n"
    "    pass\n"
    "\n"
    "\n"
    "class DeployerTestCase:\n"
    "    def assert_lines(self, output, *lines):\n"
    "        pass\n"
    "\n"
    "    def assert_refusal(self, output, error, *steps):\n"
    "        pass\n"
)
LOOSE = (
    " checks an ERROR line apart from the line after it; check a refusal with assert_refusal, which holds each step "
    "right after its ERROR line, or state why the lines are apart in LINES_APART_ALLOWED"
)
NO_HARNESS = (
    "tests/deployer/harness.py defines no assert_lines, so no deployer suite's assertion could be read; the policy "
    "checked nothing"
)


def suite(*body: str, head: str = "from harness import DeployerTestCase\n") -> str:
    """A deployer suite whose one test holds the body's lines, the first of them on line 6."""
    return (
        head
        + "\n\nclass Tests(DeployerTestCase):\n    def test_it(self):\n"
        + "".join(f"        {line}\n" for line in body)
    )


class LooseRefusalAssertionFixtures(unittest.TestCase):
    def problems(self, files: dict[str, str], harness: str | None = HARNESS) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root, {**({"tests/deployer/harness.py": harness} if harness is not None else {}), **files}
            )
            return loose_refusal_assertion_problems(root)

    def test_a_tree_without_the_harness_assertion_fails_rather_than_passing_as_checked(self) -> None:
        loose = {"tests/deployer/test_loose.py": suite('self.assert_lines(out, "ERROR: Bad", "Fix it.")')}
        for label, files, harness, expected in (
            ("no deployer suites", {}, None, []),
            ("no harness", loose, None, ["tests/deployer/test_loose.py:6 assert_lines in Tests.test_it" + LOOSE]),
            ("the assertion renamed", {}, HARNESS.replace("assert_lines", "assert_each_line"), []),
            ("the assertion only nested", {}, "def make():\n    def assert_lines(output, *lines):\n        pass\n", []),
        ):
            with self.subTest(label):
                self.assertEqual([*expected, NO_HARNESS], self.problems(files, harness))

    def test_a_file_that_does_not_parse_is_reported_by_name(self) -> None:
        problems = self.problems({"tests/deployer/test_broken.py": "def broken(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tests/deployer/test_broken.py does not parse: "), problems)

    def test_an_error_line_before_another_line_fails_however_the_call_spells_it(self) -> None:
        for label, body, head in (
            ("positional", ['self.assert_lines(out, "ERROR: Bad", "Fix it.")'], None),
            ("indented", ['self.assert_lines(out, "  ERROR: Bad", "  Fix it.")'], None),
            ("an f-string", ['self.assert_lines(out, f"ERROR: Bad {name}", f"Fix {name}.")'], None),
            ("format", ['self.assert_lines(out, "ERROR: Bad {}".format(name), "Fix it.")'], None),
            ("percent", ['self.assert_lines(out, "ERROR: Bad %s" % name, "Fix it.")'], None),
            ("plus", ['self.assert_lines(out, "ERROR: Bad " + name, "Fix it.")'], None),
            ("a tuple", ['self.assert_lines(out, *("ERROR: Bad", "Fix it."))'], None),
            ("a list", ['self.assert_lines(out, *["ERROR: Bad", "Fix it."])'], None),
            ("a nested unpacking", ['self.assert_lines(out, *["ERROR: Bad", *("Fix it.",)])'], None),
            ("output by keyword", ['self.assert_lines(*["ERROR: Bad", "Fix it."], output=out)'], None),
            ("output by a mapping", ['self.assert_lines(*["ERROR: Bad", "Fix it."], **{"output": out})'], None),
            ("a named line", ['refusal = "ERROR: Bad"', "self.assert_lines(out, refusal, 'Fix it.')"], None),
            ("a named list", ['lines = ["ERROR: Bad", "Fix it."]', "self.assert_lines(out, *lines)"], None),
            ("another error after it", ['self.assert_lines(out, "ERROR: Bad", "ERROR: Worse")'], None),
            ("unpacked lines after it", ['self.assert_lines(out, "ERROR: Bad", *steps)'], None),
            ("a call's lines after it", ['self.assert_lines(out, "ERROR: Bad", *steps())'], None),
            ("through the class", ['DeployerTestCase.assert_lines(self, out, "ERROR: Bad", "Fix it.")'], None),
            ("through a parameter", ['test.assert_lines(out, "ERROR: Bad", "Fix it.")'], None),
            ("through super", ['super().assert_lines(out, "ERROR: Bad", "Fix it.")'], None),
            (
                "through the module",
                ['harness.DeployerTestCase.assert_lines(self, out, "ERROR: Bad", "Fix it.")'],
                "import harness\nfrom harness import DeployerTestCase\n",
            ),
            (
                "a bare import",
                ['assert_lines(out, "ERROR: Bad", "Fix it.")'],
                "from harness import DeployerTestCase, assert_lines\n",
            ),
            (
                "a bare import renamed",
                ['found(out, "ERROR: Bad", "Fix it.")'],
                "from harness import DeployerTestCase\nfrom harness import assert_lines as found\n",
            ),
        ):
            with self.subTest(label):
                at = 7 if head and head.count("\n") == 2 else 6
                files = {"tests/deployer/test_loose.py": suite(*body, **({"head": head} if head else {}))}
                line = at + len(body) - 1
                self.assertEqual(
                    [f"tests/deployer/test_loose.py:{line} assert_lines in Tests.test_it" + LOOSE], self.problems(files)
                )

    def test_a_constant_the_module_binds_is_followed_into_its_elements(self) -> None:
        files = {
            "tests/deployer/test_loose.py": (
                'REFUSAL = "ERROR: Bad"\n'  # 1
                'REMEDY = (REFUSAL, "Fix it.")\n'  # 2
                "from harness import DeployerTestCase\n"  # 3
                "\n"  # 4
                "\n"  # 5
                "class Tests(DeployerTestCase):\n"  # 6
                "    def test_it(self):\n"  # 7
                "        self.assert_lines(out, *REMEDY)\n"  # 8
            ),
        }
        self.assertEqual(["tests/deployer/test_loose.py:8 assert_lines in Tests.test_it" + LOOSE], self.problems(files))

    def test_assert_refusal_an_error_line_alone_or_last_or_lines_with_no_error_pass(self) -> None:
        files = {
            "tests/deployer/test_conforming.py": suite(
                'self.assert_refusal(out, "ERROR: Bad", "Fix it.")',
                'self.assert_lines(out, "ERROR: Bad")',
                'self.assert_lines(out, "Recovering run...", "  ERROR: Bad")',
                'self.assert_lines(out, "  WARNING: an ERROR: inside", "Fix it.")',
                'self.assert_lines(out, "Recovering run...", *warnings)',
                'self.assert_lines(out, f"{prefix}ERROR: Bad", "Fix it.")',
                'self.assert_lines(out, *["ERROR: Bad"])',
                'self.assert_lines(out, 1, "Fix it.")',
            ),
            # A function of a suite's own, and one an import from elsewhere binds, are not the harness's assertion.
            "tests/deployer/test_own.py": (
                "from checks import lines_found as assert_lines\n"
                "\n"
                "\n"
                "def assert_listed(output, *lines):\n"
                "    pass\n"
                "\n"
                "\n"
                "def test_it():\n"
                '    assert_lines(out, "ERROR: Bad", "Fix it.")\n'
                '    assert_listed(out, "ERROR: Bad", "Fix it.")\n'
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_a_line_bound_more_than_once_is_not_read_as_an_error_line(self) -> None:
        # A stated limit: the line's text is known only when the test runs.
        files = {
            "tests/deployer/test_rebound.py": suite(
                'refusal = "ERROR: Bad"', 'refusal = "Fine."', "self.assert_lines(out, refusal, 'Fix it.')"
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_a_stated_reason_passes_and_one_no_longer_needed_or_malformed_fails(self) -> None:
        apart = suite('self.assert_lines(out, "ERROR: Unexpected defect", "Deployment failed; reconciling...")')
        files = {
            "tests/deployer/test_apart.py": (
                'LINES_APART_ALLOWED = {"Tests.test_it": "a defect\'s line has no step; recovery\'s lines follow"}\n'
                + apart
            ),
            "tests/deployer/test_gone.py": (
                'LINES_APART_ALLOWED = {"Tests.test_it": "once apart"}\n'
                + suite('self.assert_refusal(out, "ERROR: Bad", "Fix it.")')
            ),
            "tests/deployer/test_unexplained.py": 'LINES_APART_ALLOWED = {"Tests.test_it": ""}\n' + apart,
        }
        self.assertEqual(
            [
                "tests/deployer/test_unexplained.py:7 assert_lines in Tests.test_it" + LOOSE,
                "tests/deployer/test_gone.py: LINES_APART_ALLOWED allows Tests.test_it, which no longer checks an "
                "ERROR line apart from the line after it",
                "tests/deployer/test_unexplained.py: LINES_APART_ALLOWED must map each scope to the reason its lines "
                "are apart",
            ],
            self.problems(files),
        )

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

from refusal_steps import COVERED, REFUSAL, UNFOLLOWED, _Refusals, refusal_step_problems
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

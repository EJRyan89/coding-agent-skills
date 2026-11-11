"""Fixture tests for tests/validation/refusal_steps.py.

The policy fails on a deployer refusal built from one line without its module saying why, and passes on one that ends
with a step, unpacks its lines, is a cancellation, or is sanctioned beside the code.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from refusal_steps import refusal_step_problems
from validation_support import write_fixture_tree

SUFFIX = " ends at its ERROR line; add the step to take as its next line, or state why it needs none in STEP_ALLOWED"
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
                "deployer/gone.py: STEP_ALLOWED allows parse, which no longer builds a refusal from one line",
                "deployer/unexplained.py: STEP_ALLOWED must map each scope to the reason its refusals need no step",
            ],
            self.problems(files),
        )

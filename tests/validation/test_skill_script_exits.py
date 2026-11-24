"""Fixture tests for the script-results policy in tests/validation/skill_scripts.py: the exit codes a skill script gives
through a name, a local, an annotated, or an imported constant, read through ejryan89-skill-core's exit_constants, a
name it cannot read, and run_main's Ctrl+C 130. They sit apart from test_skill_scripts.py because that suite is at its
shard cap, so its recorded tests keep a shard of their own.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_scripts import script_contract_problems
from validation_support import write_fixture_tree

SCRIPT_RESULTS_DOC = '"Script results" in docs/adding-a-skill.md'


def mark_skills(root: Path) -> Path:
    """Give each skills/<name> folder with a scripts/ directory the SKILL.md that makes it a skill, and return root."""
    for scripts in (root / "skills").glob("*/scripts"):
        (scripts.parent / "SKILL.md").write_text(f"# {scripts.parent.name}\n", encoding="utf-8")
    return root


def exits(code: int) -> str:
    """The breach the script-results policy reports for an exit with `code`, written out, never read from the policy."""
    return f"exits {code}; scripts exit only 0, 1, or 2; see {SCRIPT_RESULTS_DOC}"


def exits_unread(expression: str) -> str:
    """The breach the script-results policy reports for an exit whose code it cannot read."""
    return (
        f"exits {expression}, a code validation cannot read; give 0, 1, or 2 as an int literal or a constant bound "
        f"to one; see {SCRIPT_RESULTS_DOC}"
    )


# A module of exit codes beside the script, which its imports name by its stem.
CODES_MODULE = "STALE = 4\nDONE = 0\nMADE = int('4')\n"


# The policy read an exit's code only as a literal, so a name bound to one escaped it: a local constant, an annotated
# one, an imported one, by its name or through its module, and a conditional between two of them. A name the reader
# cannot settle fails as unread, never passes. 130 passes only where an except clause that names KeyboardInterrupt
# gives it, as run_main does. A case that expects no breach passes.
@pytest.mark.parametrize(
    ("files", "breaches"),
    [
        pytest.param(
            {"run.py": "import sys\nSTALE = 4\ndef main():\n    sys.exit(STALE)\n"}, [exits(4)], id="a local constant"
        ),
        pytest.param(
            {"run.py": "STALE = 4\ndef main():\n    return STALE\n"}, [exits(4)], id="main returns a constant"
        ),
        pytest.param(
            {"run.py": "from typing import Final\nSTALE: Final[int] = 4\ndef main() -> int:\n    return STALE\n"},
            [exits(4)],
            id="an annotated constant",
        ),
        pytest.param(
            {"run.py": "import sys\nSTALE = DONE = 4\ndef main():\n    raise SystemExit(DONE)\n"},
            [exits(4)],
            id="a chained constant raised",
        ),
        pytest.param(
            {"run.py": "from codes import STALE\ndef main():\n    return STALE\n", "codes.py": CODES_MODULE},
            [exits(4)],
            id="an imported constant",
        ),
        pytest.param(
            {"run.py": "import codes, sys\ndef main():\n    sys.exit(codes.STALE)\n", "codes.py": CODES_MODULE},
            [exits(4)],
            id="a constant through its module",
        ),
        pytest.param(
            {"run.py": "import sys\nSTALE = 4\ndef main(late):\n    sys.exit(STALE if late else 0)\n"},
            [exits(4)],
            id="a conditional's arm",
        ),
        pytest.param(
            {
                "run.py": "import sys\nif sys.platform == 'win32':\n    STALE = 3\nelse:\n    STALE = 4\n"
                "def main():\n    return STALE\n"
            },
            [exits(3), exits(4)],
            id="each value a constant may hold",
        ),
        pytest.param(
            {"run.py": "INTERRUPTED = 130\ndef main():\n    return INTERRUPTED\n"},
            [exits(130)],
            id="130 outside a Ctrl+C handler",
        ),
        pytest.param(
            {
                "run.py": "import sys\nGONE = 131\ndef main():\n    try:\n        pass\n    except KeyboardInterrupt:\n"
                "        sys.exit(GONE)\n"
            },
            [exits(131)],
            id="another code in a Ctrl+C handler",
        ),
        pytest.param(
            {"run.py": "import sys\nSTALE = int('4')\ndef main():\n    sys.exit(STALE)\n"},
            [exits_unread("STALE")],
            id="a constant bound to a call",
        ),
        pytest.param(
            {"run.py": "from codes import MADE\ndef main():\n    return MADE\n", "codes.py": CODES_MODULE},
            [exits_unread("MADE")],
            id="an imported constant bound to a call",
        ),
        pytest.param(
            {"run.py": "import sys\nfrom elsewhere import STALE\ndef main():\n    sys.exit(STALE)\n"},
            [exits_unread("STALE")],
            id="a constant of a module not found",
        ),
        pytest.param(
            {"run.py": "import codes\ndef main():\n    return codes.GONE\n", "codes.py": CODES_MODULE},
            [exits_unread("codes.GONE")],
            id="a name its module does not bind",
        ),
        pytest.param({"run.py": "def main():\n    return GONE\n"}, [exits_unread("GONE")], id="a name nothing binds"),
        pytest.param(
            {"run.py": "from codes import STALE\ndef main():\n    return STALE\n", "codes.py": "STALE = (\n"},
            [exits_unread("STALE")],
            id="a constant of a module that does not parse",
        ),
        pytest.param(
            {"run.py": "from codes import STALE\ndef main():\n    return STALE\n", "codes/__init__.py": CODES_MODULE},
            [exits(4)],
            id="a package's constant",
        ),
        pytest.param(
            {
                "run.py": "import sys\nINTERRUPTED = 130\ndef main():\n    try:\n        pass\n    except:\n"
                "        sys.exit(INTERRUPTED)\n"
            },
            [exits(130)],
            id="130 in a clause that names no exception",
        ),
        pytest.param(
            {
                "run.py": "import sys\nINTERRUPTED = 130\ndef main():\n    try:\n        pass\n"
                "    except BaseException:\n        sys.exit(INTERRUPTED)\n"
            },
            [exits(130)],
            id="130 in a handler of BaseException",
        ),
        # A name bound to a contract code passes.
        pytest.param(
            {"run.py": "OK = 0\nFAILED: int = 1\ndef main(bad):\n    return FAILED if bad else OK\n"}, [], id="0 and 1"
        ),
        pytest.param(
            {"run.py": "import codes, sys\ndef main():\n    sys.exit(codes.DONE)\n", "codes.py": CODES_MODULE},
            [],
            id="an imported 0",
        ),
        pytest.param(
            {
                "run.py": "from codes import DONE\ndef main():\n    return DONE\n",
                "../../ejryan89-skill-core/scripts/codes.py": CODES_MODULE,
            },
            [],
            id="ejryan89-skill-core's constant",
        ),
        # A name a function binds itself, a local or a parameter, holds a value the run computes, which the exit
        # tables follow; the policy reads the module's constants alone.
        pytest.param({"run.py": "STALE = 4\ndef main(STALE):\n    return STALE\n"}, [], id="a parameter"),
        pytest.param({"run.py": "def main(run):\n    code = run()\n    return code\n"}, [], id="a local"),
        pytest.param(
            {
                "run.py": "import sys\nINTERRUPTED = 130\ndef main():\n    try:\n        pass\n"
                "    except (OSError, KeyboardInterrupt):\n        sys.exit(INTERRUPTED)\n"
            },
            [],
            id="130 in a Ctrl+C handler",
        ),
        pytest.param(
            {
                "run.py": "import builtins\ndef main():\n    try:\n        pass\n"
                "    except builtins.KeyboardInterrupt:\n        return 130\n"
            },
            [],
            id="130 in a handler of builtins.KeyboardInterrupt",
        ),
        pytest.param(
            {"run.py": "import sys\ndef main():\n    sys.exit('FAILED x')\n    return True\n"}, [], id="a message"
        ),
    ],
)
def test_script_contract_policy_reads_an_exit_code_given_through_a_name(
    tmp_path: Path, files: dict[str, str], breaches: list[str]
) -> None:
    write_fixture_tree(tmp_path, {f"skills/alpha/scripts/{name}": text for name, text in files.items()})
    problems = script_contract_problems(mark_skills(tmp_path))
    # The script's own breaches; a module beside it that does not parse is reported on a line of its own.
    assert breaches == [
        problem.split(" ", 1)[1] for problem in problems if problem.startswith("skills/alpha/scripts/run.py:")
    ]

"""Deploy this repository's skills, or configure them with `python deploy.py configure`. See README.md.

Exit status:
  0    the command did its job, a dry run and --help included
  1    a last line FAILED <reason>, an ERROR the deployer printed, or a Python older than the floor below
  2    a usage error
  130  cancelled, by Ctrl+C, during a recovery included, or by the end of input at a prompt
"""

# Keep this module importable by a Python 3 older than the floor, so it reaches the version check below: nothing
# from the deployer package until the check passes. ejryan89-skill-core's console module, imported first for the runner
# every entry point shares, and the flat_text module it imports need only Python 3.7. The annotations are never
# evaluated, so no annotation fails on such a Python before the check either.
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main

# deployer/tools.py's MINIMUM_PYTHON, with its reason beside it, repeated here because the check below runs before
# the deployer is importable.
MINIMUM_PYTHON = (3, 12, 0)

# The returns tests/validation/exit_tables.py cannot follow from main into the deployer, with where their codes come
# from; the table above names them.
EXIT_CODES_ALLOWED = {
    "exc.code": "argparse's ParserExit in deployer/arguments.py: 0 after --help, 2 after a usage error",
    "top.parse_known_args(arguments)": "the namespace deployer/arguments.py parses, which deployer/cli.py returns "
    "only when it is an int, so it is no exit code",
}


def require_supported_python(version_info: Sequence[int | str] = sys.version_info) -> None:
    if tuple(version_info[:3]) < MINIMUM_PYTHON:
        sys.stderr.write(
            f"\nERROR: Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer "
            f"is required; this is Python {'.'.join(map(str, version_info[:3]))}.\n"
            'See "Installing the tools" in docs/installation.md.\n\n'
        )
        raise SystemExit(1)


def main() -> int:
    require_supported_python()
    from deployer import cli, platform_support
    from deployer.paths import Paths, copilot_home

    def paths() -> Paths:
        """The paths the environment names, which cli.main reads once the command line has parsed, so --help and a
        usage error exit as the table says whatever the home or COPILOT_HOME holds."""
        return Paths(Path(__file__).resolve().parent, platform_support.home_directory(), copilot_home=copilot_home())

    return cli.main(sys.argv[1:], paths)


def traceback_requested() -> bool:
    """Whether a failure no command handled shows its traceback after the FAILED line: --debug, or DEPLOYER_DEBUG.

    Asked only once main has failed past the version check. The variable is read first, so it still shows the traceback
    of a deployer module that fails to import; the command line is read with the parser main used.
    """
    from deployer.errors import debug_requested

    if debug_requested():
        return True
    from deployer.arguments import debug_given

    return debug_given(sys.argv[1:])


if __name__ == "__main__":
    run_main(main, errors="backslashreplace", traceback_requested=traceback_requested)

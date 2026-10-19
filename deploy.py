"""Deploy this repository's skills, or configure them with `python deploy.py configure`. See README.md."""

# Keep this module importable by a Python 3 older than the floor, so it reaches the version check below: nothing
# from the deployer package until the check passes. ejryan89-skill-core's console module, imported first for the runner
# every entry point shares, and the flat_text module it imports need only Python 3.7.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main

MINIMUM_PYTHON = (3, 11, 4)


def require_supported_python(version_info=sys.version_info):
    if tuple(version_info[:3]) < MINIMUM_PYTHON:
        sys.stderr.write(
            f"\nERROR: Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer "
            f"is required; this is Python {'.'.join(map(str, version_info[:3]))}.\n"
            'See "Installing the tools" in docs/installation.md.\n\n'
        )
        raise SystemExit(1)


def main():
    require_supported_python()
    from deployer import cli, platform_support
    from deployer.paths import Paths

    home = platform_support.home_directory()
    return cli.main(sys.argv[1:], Paths(Path(__file__).resolve().parent, home))


def traceback_requested():
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

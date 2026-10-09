"""Deploy this repository's skills, or configure them with `python deploy.py configure`. See README.md."""

# Keep this module importable by a Python 3 older than the floor, so it reaches the version check below: nothing
# from the deployer package until the check passes. skill-core's console module, imported first for the console
# setup every entry point shares, needs only Python 3.7.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "skill-core" / "scripts"))

from console import use_utf8_output

MINIMUM_PYTHON = (3, 11)


def require_supported_python(version_info=sys.version_info):
    if tuple(version_info[:2]) < MINIMUM_PYTHON:
        sys.stderr.write(
            f"\nERROR: Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer "
            f"is required; this is Python {version_info[0]}.{version_info[1]}.\n"
            'See "Installing the tools" in docs/installation.md.\n\n'
        )
        raise SystemExit(1)


def main():
    require_supported_python()
    from deployer import cli, platform_support
    from deployer.paths import Paths

    home = platform_support.home_directory()
    return cli.main(sys.argv[1:], Paths(Path(__file__).resolve().parent, home))


if __name__ == "__main__":
    use_utf8_output(errors="backslashreplace")
    raise SystemExit(main())

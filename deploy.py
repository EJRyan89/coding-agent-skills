"""Deploy this repository's skills, or configure them with `python deploy.py configure`. See README.md."""

# Keep this module importable by old Pythons so they reach the version check below: no annotations
# future import, no f-strings, and nothing from the deployer package until the check passes.
import sys

MINIMUM_PYTHON = (3, 11)


def require_supported_python(version_info=sys.version_info):
    if tuple(version_info[:2]) < MINIMUM_PYTHON:
        sys.stderr.write(
            "\nERROR: Python {0}.{1} or newer is required; this is Python {2}.{3}.\n"
            'See "Installing the tools" in docs/installation.md.\n\n'.format(
                MINIMUM_PYTHON[0], MINIMUM_PYTHON[1], version_info[0], version_info[1]
            )
        )
        raise SystemExit(1)


def main():
    require_supported_python()
    from pathlib import Path

    from deployer import cli, platform_support
    from deployer.paths import Paths

    platform_support.use_utf8_output()
    home = platform_support.home_directory()
    return cli.main(sys.argv[1:], Paths(Path(__file__).resolve().parent, home))


if __name__ == "__main__":
    raise SystemExit(main())

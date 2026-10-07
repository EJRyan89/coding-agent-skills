from __future__ import annotations

import os
import sys
import traceback

# Set to anything but empty or 0, it prints tracebacks as --debug does, for runs whose command line is not at hand.
DEBUG_VARIABLE = "DEPLOYER_DEBUG"
DEBUG_HINT = "Rerun with --debug to see the traceback."
CHECK_THE_PATH = "Check that the path exists and that you can read it, then retry."

RECOVERY_GUIDE = "docs/recovery.md"
# The guide's sections that refusals point at; a test checks each is a heading of the guide.
RECOVERY_SECTIONS = (
    "Interrupted deployments",
    "When recovery fails",
    "Backups",
    "The deployment lock",
    "Ownership held by another source",
    "Deploying from another checkout",
)


def see_recovery(section: str) -> str:
    """The last line of a refusal whose remedy takes more than one line to explain."""
    if section not in RECOVERY_SECTIONS:
        raise ValueError(f"{RECOVERY_GUIDE} has no section {section!r}")
    return f'See "{section}" in {RECOVERY_GUIDE}.'


class DeployError(Exception):
    """A fail-closed deployment error whose lines are printed to stderr verbatim."""

    def __init__(self, *lines: str, exit_code: int = 1) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines
        self.exit_code = exit_code


def print_error(error: DeployError, debug: bool = False) -> None:
    """Print an error's lines to stderr between blank lines, like every other deploy.py output.

    With debug, the traceback follows them, its causes included, so an error converted from an OSError shows where
    that OSError was raised.
    """
    print("", file=sys.stderr)
    for line in error.lines:
        print(line, file=sys.stderr)
    print("", file=sys.stderr)
    if debug:
        print_traceback(error)


def print_traceback(error: BaseException) -> None:
    sys.stderr.write("".join(traceback.format_exception(error)))
    print("", file=sys.stderr)


def debug_requested(flag: bool = False) -> bool:
    """Whether to print tracebacks: --debug, or DEPLOYER_DEBUG set to anything but empty or 0. Read only here."""
    return flag or os.environ.get(DEBUG_VARIABLE, "") not in ("", "0")


def os_error(error: OSError, action: str, remedy: str = CHECK_THE_PATH) -> DeployError:
    """An OSError as the lines deploy.py ends with: what failed, on which path, why, and what to do."""
    reason = error.strerror or str(error)
    where = f"{error.filename}: " if error.filename else ""
    converted = DeployError(f"ERROR: Could not {action}: {where}{reason}", remedy, DEBUG_HINT)
    converted.__cause__ = error
    return converted


def fail(error: DeployError | OSError, debug: bool, action: str) -> int:
    """Print how a command ended before finishing, an OSError as its DeployError, and return the exit code."""
    if isinstance(error, OSError):
        error = os_error(error, action)
    print_error(error, debug)
    return error.exit_code


class Cancelled(DeployError):
    """The user cancelled at a prompt, before anything was changed."""

    def __init__(self, *lines: str) -> None:
        super().__init__(*lines, exit_code=130)

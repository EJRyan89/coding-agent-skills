from __future__ import annotations

import os
import sys
import traceback

# Set to anything but empty or 0, it prints tracebacks as --debug does, for runs whose command line is not at hand.
DEBUG_VARIABLE = "DEPLOYER_DEBUG"
DEBUG_HINT = "Rerun with --debug to see the traceback."
CHECK_THE_PATH = "Check that the path exists and that you can read it, then retry."
CANCELLED = "Cancelled; nothing was changed."
# Ctrl+C after a deployment began, which reconciles its journal before it exits.
DEPLOYMENT_CANCELLED = "Cancelled after the deployment began; its changes are reconciled before exit."

RECOVERY_GUIDE = "docs/recovery.md"
# The section a refusal of a manifest, configuration, or journal the deployer cannot read points at.
STATE_FILE = "A state file that does not read"
# The guide's sections that refusals point at; a test checks each is a heading of the guide.
RECOVERY_SECTIONS = (
    "Interrupted deployments",
    "When recovery fails",
    "Backups",
    "The deployment lock",
    "Ownership held by another source",
    "Deploying from another checkout",
    "Deploying an earlier commit",
    "A tool that does not finish",
    STATE_FILE,
)


def see_recovery(section: str) -> str:
    """The last line of a refusal whose remedy takes more than one line to explain."""
    if section not in RECOVERY_SECTIONS:
        raise ValueError(f"{RECOVERY_GUIDE} has no section {section!r}")
    return f'See "{section}" in {RECOVERY_GUIDE}.'


def input_ended(awaited: str) -> str:
    """The line before a cancellation at a prompt whose input ended, naming what the prompt waited for, so a caller
    that pipes its answers learns which one it left out."""
    return f"The input ended before {awaited}."


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
    from .platform_support import normalize

    reason = error.strerror or str(error)
    # The path in the forward-slash form the deployer prints every other path in.
    filename = normalize(error.filename) if isinstance(error.filename, str) else error.filename
    where = f"{filename}: " if filename else ""
    converted = DeployError(f"ERROR: Could not {action}: {where}{reason}", remedy, DEBUG_HINT)
    converted.__cause__ = error
    return converted


def fail(error: DeployError | OSError | KeyboardInterrupt, debug: bool, action: str) -> int:
    """Print how a command ended before finishing and return its exit code.

    An OSError is reported as its DeployError, and Ctrl+C as a cancellation, exit 130.
    """
    if isinstance(error, (OSError, KeyboardInterrupt)):
        error = converted(error, action)
    print_error(error, debug)
    return error.exit_code


def converted(error: OSError | KeyboardInterrupt, action: str, cancelled: str = CANCELLED) -> DeployError:
    """An OSError as the DeployError naming the action, path, and reason, or Ctrl+C as the cancellation, exit 130."""
    if isinstance(error, OSError):
        return os_error(error, action)
    cancellation = Cancelled(cancelled)
    cancellation.__cause__ = error
    return cancellation


def named(error: BaseException, action: str) -> str:
    """The line that names a failure: an OSError's action, path, and reason, or else the exception the deployer has no
    message for, a defect rather than a failure it expects."""
    if isinstance(error, OSError):
        return os_error(error, action).lines[0]
    return f"ERROR: Unexpected {type(error).__name__}: {error}"


class Cancelled(DeployError):
    """The user cancelled at a prompt, before anything was changed."""

    def __init__(self, *lines: str) -> None:
        super().__init__(*lines, exit_code=130)

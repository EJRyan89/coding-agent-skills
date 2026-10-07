from __future__ import annotations

import sys

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


def print_error(error: DeployError) -> None:
    """Print an error's lines to stderr between blank lines, like every other deploy.py output."""
    print("", file=sys.stderr)
    for line in error.lines:
        print(line, file=sys.stderr)
    print("", file=sys.stderr)


class Cancelled(DeployError):
    """The user cancelled at a prompt, before anything was changed."""

    def __init__(self, *lines: str) -> None:
        super().__init__(*lines, exit_code=130)

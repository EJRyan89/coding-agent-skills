"""Command-line parsers for deploy.py and its configure command."""

from __future__ import annotations

import argparse
import functools
import sys
from typing import NoReturn

from .errors import DeployError

PROG = "python deploy.py"
CONFIGURE_COMMAND = "configure"
CONFIGURE_COMMAND_LINE = f"{PROG} {CONFIGURE_COMMAND}"
CHECK_COMMAND = "check"
CHECK_COMMAND_LINE = f"{PROG} {CHECK_COMMAND}"
VERIFY_COMMAND = "verify"
VERIFY_COMMAND_LINE = f"{PROG} {VERIFY_COMMAND}"
HELP_WIDTH = 80
USAGE = (
    f"{PROG} [--all [--include NAME]] [--dry-run]\n"
    f"                        [--force] [--force-item NAME]\n"
    f"       {PROG} --migrate-from ID\n"
    f"       {PROG} --canary-home DIR [--all [--include NAME]]\n"
    f"                        [--force] [--force-item NAME]\n"
    f"       {CONFIGURE_COMMAND_LINE} [--reset]\n"
    f"       {CHECK_COMMAND_LINE}\n"
    f"       {VERIFY_COMMAND_LINE}"
)
# A fixed width keeps help identical in every terminal at least this wide, instead of re-wrapping to each one.
HelpFormatter = functools.partial(argparse.RawDescriptionHelpFormatter, width=HELP_WIDTH, max_help_position=24)


class ParserExit(Exception):
    """Raised instead of exiting the process when a parser finishes, as it does after --help."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def __init__(self, help_command: str, description: str, epilog: str | None = None) -> None:
        super().__init__(
            prog=PROG,
            usage=USAGE,
            description=description,
            epilog=epilog,
            formatter_class=HelpFormatter,
            allow_abbrev=False,
        )
        self._help_command = help_command

    def format_help(self) -> str:
        """Surround help with blank lines, like every other deploy.py output."""
        return f"\n{super().format_help()}\n"

    def error(self, message: str) -> NoReturn:
        raise DeployError(f"ERROR: {message}", f"Run '{self._help_command} --help' for usage.")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            sys.stderr.write(message)
        raise ParserExit(status)


def deploy_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        PROG,
        description="Render, validate, and deploy this repository's skills.",
        epilog=(
            "commands:\n"
            # Pad to the option column, which argparse sets from the longest option, "--migrate-from ID".
            f"  {CONFIGURE_COMMAND:<17}  set the values skills need; see its --help\n"
            f"  {CHECK_COMMAND:<17}  list the tools needed and which are missing\n"
            f"  {VERIFY_COMMAND:<17}  check that Codex and Copilot CLI find the adapters"
        ),
    )
    parser.add_argument("--all", dest="select_all", action="store_true",
                        help="deploy everything except uninstalled opt-in items")
    parser.add_argument("--include", action="append", default=[], metavar="NAME",
                        help="with --all, also deploy this opt-in item; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    parser.add_argument("--force", action="store_true", help="replace modified or unmanaged items (backed up)")
    parser.add_argument("--force-item", dest="force_items", action="append", default=[], metavar="NAME",
                        help="replace one item (backed up); repeatable")
    parser.add_argument("--migrate-from", default="", metavar="ID",
                        help="take over items from another source ID")
    parser.add_argument("--canary-home", default="", metavar="DIR",
                        help="deploy into a throwaway home under the temp directory")
    return parser


def configure_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        CONFIGURE_COMMAND_LINE,
        description=(
            "Set or change the configuration values skills need.\n"
            "At each prompt, Enter keeps the current value and Ctrl+C cancels."
        ),
    )
    parser.add_argument("--reset", action="store_true", help="start from an empty configuration")
    return parser


def check_parser() -> argparse.ArgumentParser:
    return _Parser(
        CHECK_COMMAND_LINE,
        description=(
            "List the tools the deployer and the skills need, with their versions,\n"
            "and which are missing or outdated. Nothing is changed."
        ),
    )


def verify_parser() -> argparse.ArgumentParser:
    return _Parser(
        VERIFY_COMMAND_LINE,
        description=(
            "Check that Codex CLI and Copilot CLI, whichever are installed, find every\n"
            "deployed runtime adapter under ~/.agents/skills, enabled and not shadowed\n"
            "by another skill of the same name. No model is started; nothing is changed."
        ),
    )

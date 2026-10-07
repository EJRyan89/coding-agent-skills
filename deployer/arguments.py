"""deploy.py's one command-line parser: the deploy options, and the configure, check, and verify subcommands."""

from __future__ import annotations

import argparse
import functools
import sys
from typing import Any, NoReturn

from .errors import DEBUG_VARIABLE, DeployError, print_error

PROG = "python deploy.py"
CONFIGURE_COMMAND = "configure"
CONFIGURE_COMMAND_LINE = f"{PROG} {CONFIGURE_COMMAND}"
CHECK_COMMAND = "check"
CHECK_COMMAND_LINE = f"{PROG} {CHECK_COMMAND}"
VERIFY_COMMAND = "verify"
VERIFY_COMMAND_LINE = f"{PROG} {VERIFY_COMMAND}"
HELP_WIDTH = 80
# A usage error, before any work, as the repository's scripts report one.
USAGE_ERROR = 2
# A fixed width keeps help identical in every terminal at least this wide, instead of re-wrapping to each one.
HelpFormatter = functools.partial(argparse.RawDescriptionHelpFormatter, width=HELP_WIDTH, max_help_position=24)


class ParserExit(Exception):
    """Raised instead of exiting the process when a parser finishes, as it does after --help."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def __init__(self, **keywords: Any) -> None:
        keywords.setdefault("formatter_class", HelpFormatter)
        keywords.setdefault("allow_abbrev", False)
        super().__init__(**keywords)
        # Filled in on the top-level parser only: the deploy options, and each subcommand's parser by name.
        self.deploy_options: list[argparse.Action] = []
        self.commands: dict[str, argparse.ArgumentParser] = {}

    def format_help(self) -> str:
        """Surround help with blank lines, like every other deploy.py output."""
        return f"\n{super().format_help()}\n"

    def error(self, message: str) -> NoReturn:
        raise DeployError(f"ERROR: {message}", f"Run '{self.prog} --help' for usage.", exit_code=USAGE_ERROR)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            sys.stderr.write(message)
        raise ParserExit(status)


def _add_debug(parser: argparse.ArgumentParser, default: Any = False) -> None:
    parser.add_argument(
        "--debug",
        action="store_true",
        default=default,
        help=f"print the traceback when it fails; or set {DEBUG_VARIABLE}=1",
    )


def _add_command(top: _Parser, commands: Any, name: str, summary: str, description: str) -> argparse.ArgumentParser:
    command: argparse.ArgumentParser = commands.add_parser(
        name, prog=f"{PROG} {name}", help=summary, description=description
    )
    top.commands[name] = command
    return command


def parser() -> _Parser:
    """The whole command line: deploying is the default, and configure, check, and verify are subcommands."""
    top = _Parser(prog=PROG, description="Render, validate, and deploy this repository's skills.")
    top.deploy_options = [
        top.add_argument(
            "--all", dest="select_all", action="store_true", help="deploy everything except uninstalled opt-in items"
        ),
        top.add_argument(
            "--include",
            action="append",
            default=[],
            metavar="NAME",
            help="with --all, also deploy this opt-in item; repeatable",
        ),
        top.add_argument("--dry-run", action="store_true", help="show what would change; change nothing"),
        top.add_argument("--force", action="store_true", help="replace modified or unmanaged items (backed up)"),
        top.add_argument(
            "--force-item",
            dest="force_items",
            action="append",
            default=[],
            metavar="NAME",
            help="replace one item (backed up); repeatable",
        ),
        top.add_argument("--migrate-from", default="", metavar="ID", help="take over items from another source ID"),
        top.add_argument(
            "--take-over-source",
            action="store_true",
            help="deploy from this checkout in place of the recorded one",
        ),
        top.add_argument(
            "--canary-home", default="", metavar="DIR", help="deploy into a throwaway home under the temp directory"
        ),
    ]
    _add_debug(top)
    commands = top.add_subparsers(
        dest="command",
        metavar="COMMAND",
        title="commands",
        help="run one of these instead of deploying",
        parser_class=_Parser,
    )
    configure = _add_command(
        top,
        commands,
        CONFIGURE_COMMAND,
        "set the values skills need; see its --help",
        "Set or change the configuration values skills need.\n"
        "At each prompt, Enter keeps the current value and Ctrl+C cancels.",
    )
    configure.add_argument("--reset", action="store_true", help="start from an empty configuration")
    _add_command(
        top,
        commands,
        CHECK_COMMAND,
        "list the tools needed and which are missing",
        "List the tools the deployer and the skills need, with their versions,\n"
        "and which are missing or outdated. Nothing is changed.",
    )
    _add_command(
        top,
        commands,
        VERIFY_COMMAND,
        "check that Codex and Copilot CLI find the adapters",
        "Check that Codex CLI and Copilot CLI, whichever are installed, find every\n"
        "deployed runtime adapter under ~/.agents/skills, enabled and not shadowed\n"
        "by another skill of the same name. No model is started; nothing is changed.",
    )
    for command in top.commands.values():
        # Suppressed, so a --debug given before the command is not reset by the command's own default.
        _add_debug(command, default=argparse.SUPPRESS)
    return top


def _parse(arguments: list[str]) -> argparse.Namespace:
    top = parser()
    namespace, extras = top.parse_known_args(arguments)
    # Report leftovers through the command they were given to, so the error points at that command's help.
    owner = top if namespace.command is None else top.commands[namespace.command]
    if extras:
        owner.error(f"unrecognized arguments: {' '.join(extras)}")
    if namespace.command is not None:
        for action in top.deploy_options:
            if getattr(namespace, action.dest) != action.default:
                top.error(f"{action.option_strings[0]} cannot be combined with the {namespace.command} command")
    return namespace


def parse_command(arguments: list[str]) -> argparse.Namespace | int:
    """deploy.py's parsed command line, or its exit code once help or a usage error has been printed."""
    try:
        return _parse(arguments)
    except ParserExit as exc:
        return exc.code
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code

"""The single command-line entry point: deploy by default, or run the configure, check, or verify command."""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from . import check, configure, pipeline, verify
from .arguments import CHECK_COMMAND, CONFIGURE_COMMAND, VERIFY_COMMAND, parse_command
from .errors import DeployError, print_error
from .paths import Paths


def main(arguments: list[str], paths: Paths | Callable[[], Paths], stdin: TextIO | None = None) -> int:
    """Run the command `arguments` name, on `paths`, or on the paths a function builds from the environment, such as
    the home and COPILOT_HOME, called once the command line has parsed: --help and a usage error end as argparse ends
    them whatever the environment holds, and every command after them is refused by the function's refusal."""
    namespace = parse_command(arguments)
    if isinstance(namespace, int):
        return namespace
    if not isinstance(paths, Paths):
        try:
            paths = paths()
        except DeployError as error:
            print_error(error)
            return error.exit_code
    if namespace.command == CONFIGURE_COMMAND:
        return configure.execute(namespace, paths, stdin)
    if namespace.command == CHECK_COMMAND:
        return check.execute(namespace, paths)
    if namespace.command == VERIFY_COMMAND:
        return verify.execute(namespace, paths)
    return pipeline.execute(namespace, paths, stdin=stdin)

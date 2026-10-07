"""The single command-line entry point: deploy by default, or run the configure, check, or verify command."""

from __future__ import annotations

from typing import TextIO

from . import check, configure, pipeline, verify
from .arguments import CHECK_COMMAND, CONFIGURE_COMMAND, VERIFY_COMMAND, parse_command
from .paths import Paths


def main(arguments: list[str], paths: Paths, stdin: TextIO | None = None) -> int:
    namespace = parse_command(arguments)
    if isinstance(namespace, int):
        return namespace
    if namespace.command == CONFIGURE_COMMAND:
        return configure.execute(namespace, paths, stdin)
    if namespace.command == CHECK_COMMAND:
        return check.execute(namespace, paths)
    if namespace.command == VERIFY_COMMAND:
        return verify.execute(namespace, paths)
    return pipeline.execute(namespace, paths, stdin=stdin)

"""The single command-line entry point: deploy by default, or run the configure, check, or verify command."""

from __future__ import annotations

from typing import TextIO

from . import check, configure, pipeline, verify
from .arguments import CHECK_COMMAND, CONFIGURE_COMMAND, VERIFY_COMMAND
from .paths import Paths


def main(arguments: list[str], paths: Paths, stdin: TextIO | None = None) -> int:
    if arguments[:1] == [CONFIGURE_COMMAND]:
        return configure.run(arguments[1:], paths, stdin)
    if arguments[:1] == [CHECK_COMMAND]:
        return check.run(arguments[1:], paths)
    if arguments[:1] == [VERIFY_COMMAND]:
        return verify.run(arguments[1:], paths)
    return pipeline.run(arguments, paths, stdin=stdin)

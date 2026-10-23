"""Interactive, atomic creation of the deployer configuration."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from . import config, fsops, platform_support, source
from .arguments import CONFIGURE_COMMAND, PROG, parse_command
from .errors import Cancelled, DeployError, debug_requested, fail, input_ended, os_error, print_error
from .paths import Paths, validate_managed_roots

# Ctrl+C or the end of input at a prompt. The configuration is written atomically, so it is unchanged.
CANCELLED = "Configuration cancelled; existing config was not changed."


def _prompt(key: str, description: str, current: str, stdin: TextIO) -> str | None:
    """Ask for one value on stderr, which carries every prompt, so stdout holds only results; None at the end of
    input."""
    sys.stdout.flush()
    print("", file=sys.stderr)
    print(f"{key}: {description}", file=sys.stderr)
    if current:
        print(f"  Current: {current}", file=sys.stderr)
    label = "New value (Enter keeps the current value, Ctrl+C cancels)" if current else "Value (Ctrl+C cancels)"
    print(f"  {label}: ", end="", file=sys.stderr, flush=True)
    # Ctrl+C reaches execute as KeyboardInterrupt; only the end of input returns None.
    line = stdin.readline()
    if not line:
        return None
    value = line.rstrip("\r\n")
    return value if value else current


def run(arguments: list[str], paths: Paths, stdin: TextIO | None = None) -> int:
    """`python deploy.py configure` with these arguments."""
    namespace = parse_command([CONFIGURE_COMMAND, *arguments])
    return namespace if isinstance(namespace, int) else execute(namespace, paths, stdin)


def execute(namespace: argparse.Namespace, paths: Paths, stdin: TextIO | None = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    reset, debug = namespace.reset, debug_requested(namespace.debug)
    try:
        platform_support.ensure_supported()
        validate_managed_roots(paths)
        source_id = source.load_source_id(paths)
        config_file = paths.config_file(source_id)
        existing: dict[str, str] = {}
        dropped: list[str] = []
        if not reset and config_file.is_file():
            try:
                existing = config.read(config_file, source_id, dropped)
            except OSError as exc:
                raise os_error(exc, "read the configuration") from exc
        print("")
        print(f"Source: {source.label(source_id, source.load_source_name(paths))}")
        print(f"Config: {platform_support.normalize(config_file)}")
        if dropped:
            sys.stdout.flush()
            for key in dropped:
                print(f"NOTE: Dropping {key}, which this deployer no longer reads.", file=sys.stderr)
        values = {key: existing[key] for key in config.CONFIGURED_VARIABLES if key in existing}
        for key, description in config.PROMPTS.items():
            # A key the saved configuration lacks offers its default, the value a deployment already takes for it.
            answer = _prompt(key, description, existing.get(key, config.DEFAULTS.get(key, "")), stdin)
            if answer is None:
                raise Cancelled(input_ended(f"a value for {key}"), CANCELLED)
            if key in config.DIRECTORY_VARIABLES:
                answer = platform_support.normalize_path_input(answer)
            elif key in config.CHOICE_VARIABLES:
                answer = answer.strip()
            values[key] = answer
        lines = [f"{config.SOURCE_KEY}={source_id}"]
        lines += [f"{key}={values[key]}" for key in config.CONFIGURED_VARIABLES if values.get(key)]
        content = "\n".join(lines) + "\n"
        config.validate_directories(config.parse(content, source_id))
        # The directory is made only for the write, so a run that stops before it leaves a fresh home as it was.
        fsops.make_directories(config_file.parent)
        fsops.write_atomic(config_file, content.encode("utf-8"))
    except KeyboardInterrupt:
        print_error(Cancelled(CANCELLED), debug)
        return 130
    except (DeployError, OSError) as exc:
        return fail(exc, debug, "write the configuration")
    print("")
    print("Saved.")
    print("")
    print("Next, preview the deployment:")
    print(f"  {PROG} --all --dry-run")
    print("")
    return 0

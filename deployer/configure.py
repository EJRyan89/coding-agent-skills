"""Interactive, atomic creation of the deployer configuration."""

from __future__ import annotations

import sys
from typing import TextIO

from . import config, fsops, platform_support, source
from .arguments import ParserExit, configure_parser
from .errors import DeployError, print_error
from .paths import Paths, validate_managed_roots


def _prompt(key: str, description: str, current: str, stdin: TextIO) -> str | None:
    print("")
    print(f"{key}: {description}")
    if current:
        print(f"  Current: {current}")
    sys.stdout.flush()
    label = "New value (Enter keeps the current value, Ctrl+C cancels)" if current else "Value (Ctrl+C cancels)"
    print(f"  {label}: ", end="", file=sys.stderr, flush=True)
    try:
        line = stdin.readline()
    except KeyboardInterrupt:
        return None
    if not line:
        return None
    value = line.rstrip("\r\n")
    return value if value else current


def run(arguments: list[str], paths: Paths, stdin: TextIO | None = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    try:
        reset = configure_parser().parse_args(arguments).reset
        platform_support.ensure_supported()
        validate_managed_roots(paths)
        source_id = source.load_source_id(paths)
        config_file = paths.config_file(source_id)
        fsops.make_directories(config_file.parent)
        existing: dict[str, str] = {}
        if not reset and config_file.is_file():
            existing = config.read(config_file, source_id)
        print("")
        print(f"Source: {source.label(source_id, source.load_source_name(paths))}")
        print(f"Config: {platform_support.normalize(config_file)}")
        values = {key: existing[key] for key in config.CONFIGURED_VARIABLES if key in existing}
        for key, description in config.PROMPTS.items():
            answer = _prompt(key, description, existing.get(key, ""), stdin)
            if answer is None:
                print("", file=sys.stderr)
                print("Configuration cancelled; existing config was not changed.", file=sys.stderr)
                print("", file=sys.stderr)
                return 1
            if key in config.DIRECTORY_VARIABLES:
                answer = platform_support.normalize_path_input(answer)
            values[key] = answer
        lines = [f"{config.SOURCE_KEY}={source_id}"]
        lines += [f"{key}={values[key]}" for key in config.CONFIGURED_VARIABLES if values.get(key)]
        content = "\n".join(lines) + "\n"
        config.validate_directories(config.parse(content, source_id))
        fsops.write_private(config_file, content.encode("utf-8"))
    except ParserExit as exc:
        return exc.code
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    except OSError as exc:
        print("", file=sys.stderr)
        print(f"ERROR: Could not write configuration: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1
    print("")
    print("Saved.")
    print("")
    print("Next, preview the deployment:")
    print("  python deploy.py --all --dry-run")
    print("")
    return 0

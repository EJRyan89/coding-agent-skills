"""Command-line interface for the structured code-review flag store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))

from review_flags import FlagError, add_flag, default_flags_path, load_store, resolve_flag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=default_flags_path(),
                        help="defaults to CODE_REVIEW_FLAGS or the standard flag store")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("category")
    add.add_argument("body")
    add.add_argument("--repository")
    add.add_argument("--pull", type=int)
    add.add_argument("--review-version", type=int,
                     help="the version of the review whose report gave the finding ID; required with --finding")
    add.add_argument("--finding")
    commands.add_parser("list")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("flag_id")
    resolve.add_argument("resolution")
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "add":
            value = add_flag(
                args.store,
                category=args.category,
                body=args.body,
                repository=args.repository,
                pull_number=args.pull,
                review_version=args.review_version,
                finding_id=args.finding,
            )
        elif args.command == "resolve":
            value = resolve_flag(args.store, args.flag_id, args.resolution)
        else:
            value = [item for item in load_store(args.store)["flags"] if item["status"] == "open"]
        print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except (FlagError, OSError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    # Output echoes flag bodies and resolutions as written; a Windows pipe's legacy code page cannot encode them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

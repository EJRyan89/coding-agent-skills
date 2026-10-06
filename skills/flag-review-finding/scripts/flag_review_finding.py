"""Command-line interface for the structured code-review flag store.

Every command prints one fact per line:
  add      ADDED <id>
  resolve  RESOLVED <id>, or ALREADY_RESOLVED <id> when the flag was resolved earlier; it keeps that resolution
  list     FLAG <id> <category> <target> <body> per open flag, then COUNT <n>. The target is
           <owner/repo>#<pull>, followed by v<version> and the finding when the flag names them, or - when it
           names no repository or pull request. The body is collapsed to one line and cut to 160 characters.
A failure prints `FAILED <reason>` and exits 1; a usage error exits 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))

from review_config import ConfigurationError
from review_flags import FlagError, add_flag, default_flags_path, load_store, resolve_flag
from review_io import PersistenceError

BODY_LIMIT = 160


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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


def one_line(text: str) -> str:
    return " ".join(text.split())


def target(flag: dict[str, Any]) -> str:
    """Where the flag points: owner/repo#pull, then the review version and finding it names, or -."""
    text = (flag["repository"] or "") + (f"#{flag['pull_number']}" if flag["pull_number"] is not None else "")
    if flag["review_version"] is not None:
        text += f" v{flag['review_version']}"
    if flag["finding_id"]:
        text += f" {one_line(flag['finding_id'])}"
    return text.strip() or "-"


def flag_line(flag: dict[str, Any]) -> str:
    body = one_line(flag["body"])
    if len(body) > BODY_LIMIT:
        body = body[: BODY_LIMIT - 1].rstrip() + "…"
    return f"FLAG {flag['id']} {one_line(flag['category'])} {target(flag)} {body}"


def run(args: argparse.Namespace) -> list[str]:
    if args.command == "add":
        flag = add_flag(
            args.store,
            category=args.category,
            body=args.body,
            repository=args.repository,
            pull_number=args.pull,
            review_version=args.review_version,
            finding_id=args.finding,
        )
        return [f"ADDED {flag['id']}"]
    if args.command == "resolve":
        existing = [item for item in load_store(args.store)["flags"] if item["id"] == args.flag_id]
        if len(existing) == 1 and existing[0]["status"] == "resolved":
            return [f"ALREADY_RESOLVED {args.flag_id}"]
        return [f"RESOLVED {resolve_flag(args.store, args.flag_id, args.resolution)['id']}"]
    flags = [item for item in load_store(args.store)["flags"] if item["status"] == "open"]
    return [*(flag_line(flag) for flag in flags), f"COUNT {len(flags)}"]


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        lines = run(args)
    except (FlagError, ConfigurationError, PersistenceError, OSError) as exc:
        print(f"FAILED {one_line(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    # Output echoes flag bodies as written; a Windows pipe's legacy code page cannot encode them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

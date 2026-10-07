"""Command-line interface for the structured code-review flag store.

Every command prints one fact per line:
  add       ADDED <id>. A flag that names a finding is refused unless that review of the pull request is archived
            and has that finding.
  resolve   RESOLVED <id>, or ALREADY_RESOLVED <id> when the flag was resolved earlier; it keeps that resolution
  list      FLAG <id> <category> <target> <body> per open flag, then COUNT <n>. The target is
            <owner/repo>#<pull>, followed by v<version> and the finding when the flag names them, or - when it
            names no repository or pull request. The body is collapsed to one line and cut to 160 characters.
  findings  FINDING v<version> <finding> <severity> <path>:<line> <headline> per open or unverified finding of a
            pull request, labelled as its review report labels it, then COUNT <n>. The headline is the finding's
            title, or its body cut like a flag's.
A failure prints `FAILED <reason>` and exits 1; a usage error exits 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import use_utf8_output
from review_archive import ArchiveError, pull_directory, pull_records, record_paths
from review_config import ConfigurationError, load_config
from review_flags import FlagError, add_flag, default_flags_path, load_store, resolve_flag, validate_target
from review_io import PersistenceError
from review_records import RecordError, carried_findings, validate_record_pair

BODY_LIMIT = 160


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--store",
        type=Path,
        default=default_flags_path(),
        help="defaults to CODE_REVIEW_FLAGS or the standard flag store",
    )
    parser.add_argument(
        "--config", type=Path, help="the code-review configuration naming the archive; defaults to CODE_REVIEW_CONFIG"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("category")
    add.add_argument("body")
    add.add_argument("--repository")
    add.add_argument("--pull", type=int)
    add.add_argument(
        "--review-version",
        type=int,
        help="the version of the review whose report gave the finding ID; required with --finding",
    )
    add.add_argument("--finding")
    commands.add_parser("list")
    findings = commands.add_parser("findings")
    findings.add_argument("--repository", required=True)
    findings.add_argument("--pull", type=int, required=True)
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


def cut(text: str) -> str:
    """Text on one line, cut to BODY_LIMIT characters."""
    text = one_line(text)
    return text if len(text) <= BODY_LIMIT else text[: BODY_LIMIT - 1].rstrip() + "…"


def flag_line(flag: dict[str, Any]) -> str:
    return f"FLAG {flag['id']} {one_line(flag['category'])} {target(flag)} {cut(flag['body'])}"


def check_finding(config: Path | None, repository: str, pull: int, version: int, finding_id: str) -> None:
    """Refuse a finding that review `version` of the pull request, as archived, does not have."""
    directory = pull_directory(Path(load_config(config)["archive_root"]), repository, pull)
    json_path, markdown_path = record_paths(directory, version)
    if not json_path.exists():
        raise FlagError(f"{repository}#{pull} has no review v{version} in the archive")
    if finding_id not in {finding["id"] for finding in validate_record_pair(json_path, markdown_path)["findings"]}:
        raise FlagError(f"Review v{version} of {repository}#{pull} has no finding {finding_id}")


def finding_lines(config: Path | None, repository: str, pull: int) -> list[str]:
    """The open and unverified findings of a pull request's finding ledger, each under the label its report shows:
    the version and ID where it first appeared, and the place it was last reported."""
    records = pull_records(Path(load_config(config)["archive_root"]), repository, pull)
    if not records:
        raise FlagError(f"{repository}#{pull} has no review in the archive")
    lines = [
        f"FINDING {item['id'].replace(':', ' ')} {item['severity']} {item['path']}:{item['line']} "
        f"{cut(item.get('title') or item['body'])}"
        for item in carried_findings(records)
    ]
    return [*lines, f"COUNT {len(lines)}"]


def run(args: argparse.Namespace) -> list[str]:
    if args.command == "findings":
        return finding_lines(args.config, args.repository, args.pull)
    if args.command == "add":
        if args.finding is not None:
            validate_target(args.repository, args.pull, args.review_version, args.finding)
            check_finding(args.config, args.repository, args.pull, args.review_version, args.finding)
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
    except (FlagError, ConfigurationError, PersistenceError, RecordError, ArchiveError, OSError) as exc:
        print(f"FAILED {one_line(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main())

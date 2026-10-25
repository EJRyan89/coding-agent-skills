"""A reviewer's way to search the head commit's source, and to the rest of it when its run's snapshot is lazy.

On the checkout route, `prepare` writes only the changed files and the analyzer settings into the snapshot and lists
every other path the snapshot can hold, with its blob id, in the snapshot's manifest. A reviewer whose prompt names
these commands reads any other file, and searches the commit, through them. A snapshot that holds every file already
has only the search, over the snapshot's own files, which serves a reviewer the runtime gives no Grep or Glob, as
Claude Code does not give a subagent that lists Bash on macOS, Linux, and WSL:

  python -B "<this script>" source-file --run "<run>" --role "<role>" --path="<path>"
      Writes the head file at <path> into the snapshot from the commit, by the blob id the manifest lists, and
      prints `SOURCE_FILE <absolute path>` to Read; `EXCLUDED <path> <reason>` when the snapshot leaves it out; or
      `FAILED <reason>`, as for a path the commit does not hold. The file is counted as read by the role.
  python -B "<this script>" source-search --run "<run>" --role "<role>" --pattern="<pattern>"
      Prints `MATCH <path>:<line>: <text>` for each line of the commit's files that matches the extended regular
      expression, in the paths the snapshot can hold, then `MATCHES <n>`, or `MATCHES more than <n>` when it stopped.

The reviewer guard allows exactly these two commands for a reviewer's own run and role (see review_guard.py). The
blob is read from the repository the run names in `run.json` as `source_repository`: the configured checkout, or the
copy of a fixture's repository in the run; a run that names none has a whole snapshot. Each command prints one fact
per line and exits 0, or 1 after `FAILED`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_writer
from console import run_main
from executable_locator import PYTHON
from review_guard import ROLE_ID, RUN_FILE, RUN_PREFIX, read_log
from review_io import PersistenceError, read_json
from review_runtime import RuntimeContractError, fetch_source_file, search_source
from typed_fields import FieldError, FieldReader

SCRIPT = Path(__file__).resolve()
# Python named as review-prs's rendered grant names it, by the platform's command.
FILE_COMMAND = PYTHON + ' -B "{script}" source-file --run "{run}" --role "{role}" --path="<path>"'
SEARCH_COMMAND = PYTHON + ' -B "{script}" source-search --run "{run}" --role "{role}" --pattern="<pattern>"'


class SourceError(ValueError):
    pass


class SourceFieldError(FieldError, SourceError):
    """A run or request field of the wrong kind."""


READ = FieldReader(SourceFieldError)


def source_commands(run: Path, role: str, *, script: Path | str = SCRIPT) -> tuple[str, str]:
    """The two commands a role's prompt names, with <path> and <pattern> for the reviewer to fill in. `script` is this
    file, spelled as the prompt should give it."""
    return (
        FILE_COMMAND.format(script=script, run=run, role=role),
        SEARCH_COMMAND.format(script=script, run=run, role=role),
    )


def _run(run: Path, role: str) -> tuple[Path | None, str, str, Path]:
    """The run's source repository, None for a whole snapshot, its request's repository and head, and the role's read
    log, once the run is a prepared review run and the role is one of its roles."""
    run = run.resolve()
    if not run.name.startswith(RUN_PREFIX) or not (run / RUN_FILE).is_file():
        raise SourceError(f"{run} is not a review run")
    try:
        state = READ.json_object(read_json(run / RUN_FILE), "run")
        request = READ.json_object(
            read_json(Path(READ.string(state.get("request_path"), "run.request_path"))), "request"
        )
        name = READ.string(request.get("repository"), "request.repository")
        pull = READ.json_object(request.get("pull_request"), "request.pull_request")
        head = READ.hex_digest(pull.get("head_sha"), "request.pull_request.head_sha", 40, 64)
        roles = READ.array(state.get("roles", []), "run.roles", READ.json_object)
        repository = READ.optional(READ.string, state.get("source_repository"), "run.source_repository")
    except (PersistenceError, FieldError) as exc:
        raise SourceError(f"the review run {run} could not be read: {exc}") from exc
    if not ROLE_ID.fullmatch(role) or role not in {item.get("id") for item in roles}:
        raise SourceError(f"the review run has no role {role}")
    if repository is not None and not Path(repository).is_dir():
        raise SourceError(f"the run's source repository {repository} is not a directory")
    return (None if repository is None else Path(repository)), name, head, read_log(run, role)


def _count(log: Path, relative: str) -> None:
    """Add a file to the role's read log, which only a reviewer the guard holds has: an unguarded one stays
    uncounted, as its reads do, and so does a path the JSON writer refuses."""
    try:
        line = json_writer.dumps(relative) + "\n"
    except json_writer.JsonWriteRefused:
        return
    if log.is_file():
        with contextlib.suppress(OSError), log.open("a", encoding="utf-8") as stream:
            stream.write(line)


def source_file(run: Path, role: str, relative: str) -> list[str]:
    """The file a reviewer asked for, counted by the snapshot path it prints, as the guard counts a Read of it: a
    document held as its extracted text, as a PDF is, is held beside the path the reviewer named."""
    repository, name, head, log = _run(run, role)
    if repository is None:
        raise SourceError("this run's snapshot holds every file already; read it under SOURCE_ROOT")
    source = run.resolve() / "source"
    found = fetch_source_file(repository, source, relative, repository=name, commit=head, staging=run.resolve())
    if isinstance(found, str):
        return [f"EXCLUDED {json.dumps(relative)} {found}"]
    _count(log, found.relative_to(source).as_posix())
    return [f"SOURCE_FILE {found}"]


def source_search(run: Path, role: str, pattern: str) -> list[str]:
    if not pattern or "\n" in pattern or "\r" in pattern:
        raise SourceError("the pattern must be one non-empty line")
    repository, name, head, _ = _run(run, role)
    matches, more = search_source(repository, run.resolve() / "source", pattern, repository=name, commit=head)
    lines = [f"MATCH {path}:{number}: {text}" for path, number, text in matches]
    return [*lines, f"MATCHES more than {len(lines)}; narrow the pattern" if more else f"MATCHES {len(lines)}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, argument in (("source-file", "--path"), ("source-search", "--pattern")):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--role", required=True)
        command.add_argument(argument, required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "source-file":
            lines = source_file(args.run, args.role, args.path)
        else:
            lines = source_search(args.run, args.role, args.pattern)
    except (SourceError, RuntimeContractError, OSError) as exc:
        print(f"FAILED {exc}")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    run_main(main)

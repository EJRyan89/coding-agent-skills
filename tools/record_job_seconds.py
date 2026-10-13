"""Record what each validation job took on CI's runners, from several completed validate runs' logs.

Usage:
  python tools/record_job_seconds.py [--count N] [RUN_ID ...]

tests/run_validation.py deals its jobs across the validate workflow's Windows legs by tests/validation/job_seconds.json,
so each leg gets about the same seconds of work. One run's job times differ a lot from the next run's, so this script
rewrites that table from several runs and takes each job's median: it reads the logs of every leg of each run through
`gh run view --log`, takes each `ok <seconds>s <job>` line the runner printed on a leg, never the unsharded Linux job's,
and sums a suite's shards under the suite's path. A job a run reports more than once, as a weekly run's second
interpreter and second validation step do, counts at its mean within that run. RUN_ID names the runs to read; without
any, the latest successful push runs of validate.yml on main are read, DEFAULT_RUN_COUNT of them or --count. The newest
run decides which jobs the table names: a job it does not report, such as a suite since renamed or removed, is left out,
and a job it reports that older runs lack takes the median of the runs that have it. A run that is not a completed,
successful run of the Validation workflow is refused, since a failed or cancelled run leaves jobs out; so is a
successful run whose log lacks a leg, as a rerun of one failed leg can leave, and a run whose log gh cannot read, such
as one past the retention period. One refused run refuses the record and the table is left alone. The release procedure
in docs/releasing.md runs it before tagging, so the legs are rebalanced once a release and never by a pull request's
live timings. It exits 0 when it wrote the table and 2 when it could not read the runs.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from console import run_main
from github_client import GitHubClient, GitHubError, subprocess_runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TABLE = REPOSITORY_ROOT / "tests" / "validation" / "job_seconds.json"
WORKFLOW_FILE = "validate.yml"
WORKFLOW_NAME = "Validation"
# The runs the table is taken from when none are named: five, so the median ignores the two slowest and the two fastest
# readings of a job (a contended runner, a cold cache) while the runs stay recent enough that their logs are still held
# and their jobs are the ones the next release runs.
DEFAULT_RUN_COUNT = 5
# What job_pool.py prints for each job that passed, after the timestamp gh puts before every log line.
OK_LINE = re.compile(r"^\S+ ok +(\d+(?:\.\d+)?)s (.+?)\s*$")
SHARD_SUFFIX = re.compile(r" \[shard \d+/\d+\]$")
# A leg's name in the log, from the workflow's matrix job name `Windows shard <shard>, Python <version>`. The Linux
# job runs every job unsharded beside the legs, so its lines are not a leg's and its seconds are not dealt.
LEG_NAME = re.compile(r"^Windows shard (\d+)/(\d+), ")


def job_seconds_from_log(log: str) -> dict[str, float]:
    """Each job's seconds by name from the legs' lines of `gh run view --log` output, whose lines are
    `<job>\\t<step>\\t<text>`."""
    reported: defaultdict[str, list[float]] = defaultdict(list)
    for line in log.splitlines():
        fields = line.split("\t", 2)
        found = OK_LINE.match(fields[-1]) if len(fields) == 3 and LEG_NAME.match(fields[0]) else None
        if found:
            reported[found.group(2)].append(float(found.group(1)))
    totals: defaultdict[str, float] = defaultdict(float)
    for label, seconds in reported.items():
        totals[SHARD_SUFFIX.sub("", label)] += sum(seconds) / len(seconds)
    return {name: round(seconds, 1) for name, seconds in totals.items() if round(seconds, 1) > 0}


def leg_problem(log: str) -> str:
    """What a log lacks of the matrix's legs, or an empty string when it reports every leg."""
    seen: set[tuple[int, int]] = set()
    for line in log.splitlines():
        found = LEG_NAME.match(line.split("	", 1)[0])
        if found:
            seen.add((int(found.group(1)), int(found.group(2))))
    counts = {count for _, count in seen}
    if len(counts) != 1:
        return "names no one set of legs"
    (count,) = counts
    missing = [f"{index}/{count}" for index in range(1, count + 1) if (index, count) not in seen]
    return f"lacks leg {', '.join(missing)}" if missing else ""


def combine(per_run: Mapping[int, Mapping[str, float]]) -> dict[str, float]:
    """Each job's median seconds across the runs that report it, for the jobs the newest run (highest ID) reports."""
    newest = per_run[max(per_run)]
    return {
        name: round(statistics.median(seconds[name] for seconds in per_run.values() if name in seconds), 1)
        for name in newest
    }


def render_table(runs: Sequence[int], seconds: Mapping[str, float]) -> str:
    """The table's text: the runs it came from, oldest first, and the seconds by job name, in name order."""
    return json.dumps({"runs": sorted(runs), "seconds": dict(sorted(seconds.items()))}, indent=2) + "\n"


def latest_runs(client: GitHubClient, count: int) -> list[int]:
    runs = client.json(
        [
            "run",
            "list",
            "--workflow",
            WORKFLOW_FILE,
            "--branch",
            "main",
            "--event",
            "push",
            "--status",
            "success",
            "--limit",
            str(count),
            "--json",
            "databaseId",
        ]
    )
    if not runs:
        raise GitHubError(f"no successful push run of {WORKFLOW_FILE} on main was found", kind="not_found")
    return [int(run["databaseId"]) for run in runs]


def run_seconds(client: GitHubClient, run: int) -> dict[str, float]:
    """The run's job seconds, once it is known to be a completed, successful run of the Validation workflow whose log
    reports every leg."""
    state = client.json(["run", "view", str(run), "--json", "workflowName,status,conclusion"])
    found = (state.get("workflowName"), state.get("status"), state.get("conclusion"))
    if found != (WORKFLOW_NAME, "completed", "success"):
        raise GitHubError(
            f"run {run} is {found[0]!r}, {found[1]}, {found[2] or 'no conclusion'}; record from completed, "
            f"successful {WORKFLOW_NAME} runs",
            kind="refused",
        )
    log = client.run(["run", "view", str(run), "--log"]).stdout
    seconds = job_seconds_from_log(log)
    if not seconds:
        raise GitHubError(
            f"run {run} reported no passing jobs; run validation with -v, as the validate workflow does", kind="refused"
        )
    problem = leg_problem(log)
    if problem:
        raise GitHubError(
            f"run {run}'s log {problem}, as a rerun of a failed leg leaves; record from runs whose every leg ran in "
            "one attempt",
            kind="refused",
        )
    return seconds


def main(arguments: list[str] | None = None, client: GitHubClient | None = None, table: Path = TABLE) -> int:
    parser = argparse.ArgumentParser(description="Record each validation job's median seconds from validate runs.")
    parser.add_argument("runs", nargs="*", type=int, help="the runs' IDs; the latest successful main push runs if none")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_RUN_COUNT,
        help=f"how many of the latest runs to read when none are named (default {DEFAULT_RUN_COUNT})",
    )
    options = parser.parse_args(arguments)
    if options.count < 1:
        parser.error("--count must be at least 1")
    client = client or GitHubClient(runner=functools.partial(subprocess_runner, cwd=REPOSITORY_ROOT))
    try:
        runs = sorted(set(options.runs)) if options.runs else latest_runs(client, options.count)
        per_run = {run: run_seconds(client, run) for run in runs}
    except GitHubError as error:
        print(f"Cannot read the validate runs: {error}", file=sys.stderr)
        return 2
    seconds = combine(per_run)
    table.write_text(render_table(runs, seconds), encoding="utf-8", newline="\n")
    named = ", ".join(str(run) for run in sorted(runs))
    print(
        f"Recorded {len(seconds)} jobs, {sum(seconds.values()):.1f} job-seconds, as the median of "
        f"{len(runs)} run{'s' if len(runs) != 1 else ''} ({named}) in {table.name}."
    )
    return 0


if __name__ == "__main__":
    run_main(main)
